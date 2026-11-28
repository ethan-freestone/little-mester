#!/usr/bin/env python3
"""Fixed, read-only compatibility inventory for the persistent system guest.

The SSH transport and lifecycle gate live in system-vm.py. This module only
builds the candidate-derived inventory, a fixed remote inspector, and a strict
bounded response classifier.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import re
import shlex


SYSTEM_WORKSPACE = "/workspace"
DEFAULT_WORKSPACE = "/home/mester/workspace"
OPENCODE_VERSION = "2.2.5"
OPENCODE_PACKAGE_JSON = "/usr/local/lib/node_modules/@opencode/cli/package.json"
MAX_ASSET_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 8192
MAX_FAILURE_EVIDENCE_BYTES = 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")
_SAFE_FSTYPE = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")


def _contract_module():
    path = pathlib.Path(__file__).with_name("parent-session-contract.py")
    spec = importlib.util.spec_from_file_location("guest_compat_session_contract", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protected guest session contract is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_inventory(workspace_path: str = SYSTEM_WORKSPACE) -> dict[str, dict]:
    """Return only immutable candidate assets plus the fixed workspace mount."""
    if workspace_path not in (SYSTEM_WORKSPACE, DEFAULT_WORKSPACE):
        raise ValueError("guest compatibility inspection accepts only fixed VM workspaces")
    contract = _contract_module()
    assets = contract.assets_for_workspace(workspace_path)
    plugin_assets = dict(contract.PROTECTED_PLUGIN_ASSETS)
    if workspace_path == SYSTEM_WORKSPACE:
        plugin_assets.update(contract.METRICS_PLUGIN_ASSETS)
    protected_config = json.dumps(
        {"autoupdate": False, "share": "disabled",
         "permission": {"edit": "ask", "bash": "ask"}},
        separators=(",", ":"),
    ).encode("utf-8")
    model_config = contract.PROTECTED_CONFIG_MODEL.encode("utf-8")
    payloads = {
        **{path: data for path, (data, _mode) in assets.items()},
        **plugin_assets,
        "/etc/opencode/opencode.json": protected_config,
        "/etc/opencode/opencode-model.json": model_config,
        "/etc/profile.d/little-mester-parent.sh": (
            "export HTTP_PROXY=http://10.0.2.100:3128\n"
            "export HTTPS_PROXY=http://10.0.2.100:3128\n"
            "export http_proxy=http://10.0.2.100:3128\n"
            "export https_proxy=http://10.0.2.100:3128\n").encode(),
        "/etc/apt/apt.conf.d/90little-mester-parent-proxy": (
            'Acquire::http::Proxy "http://10.0.2.100:3128";\n'
            'Acquire::https::Proxy "http://10.0.2.100:3128";\n').encode(),
        "/etc/ssh/sshd_config.d/99-little-mester-parent.conf":
            b"HostKeyAlgorithms ssh-ed25519\n",
    }
    cli_config = json.dumps({
        "plugins": ["/etc/opencode/plugins/little-mester-exit-confirm"],
        "keybinds": {"app.exit": "none", "prompt.clear": "none"},
    }, separators=(",", ":"))
    payloads["/etc/profile.d/little-mester-opencode.sh"] = (
        "export OPENCODE_CONFIG=/etc/opencode/opencode.json\n"
        f"export OPENCODE_CLI_CONFIG_CONTENT='{cli_config}'\n"
        "export OPENCODE_DISABLE_PROJECT_CONFIG=1\n").encode()
    modes = {path: int(mode, 8) for path, (_data, mode) in assets.items()}
    modes.update({path: 0o644 for path in plugin_assets})
    modes["/etc/opencode/opencode.json"] = 0o644
    modes["/etc/opencode/opencode-model.json"] = 0o644
    modes.update({
        "/etc/profile.d/little-mester-parent.sh": 0o644,
        "/etc/apt/apt.conf.d/90little-mester-parent-proxy": 0o644,
        "/etc/ssh/sshd_config.d/99-little-mester-parent.conf": 0o644,
        "/etc/profile.d/little-mester-opencode.sh": 0o644,
    })
    result: dict[str, dict] = {
        path: {
            "kind": "file",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": modes[path],
            "uid": 0,
            "gid": 0,
        }
        for path, payload in sorted(payloads.items())
    }
    result[OPENCODE_PACKAGE_JSON] = {
        "kind": "version-json",
        "version": OPENCODE_VERSION,
        "mode": 0o644,
        "uid": 0,
        "gid": 0,
    }
    if workspace_path == SYSTEM_WORKSPACE:
        result[workspace_path] = {
            "kind": "mount", "fstype": "virtiofs", "source": "workspace",
        }
    else:
        result[workspace_path] = {
            "kind": "disk-directory", "fstype": "disk-directory", "source": "root",
        }
    return result


_REMOTE_PROGRAM = r'''import hashlib,json,os,stat
MAX_FILE_BYTES=1048576
PATHS=__PATHS__
VERSION_PATH=__VERSION_PATH__
WORKSPACE=__WORKSPACE__
def safe_open(path):
    parts=path.lstrip("/").split("/")
    fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=nextfd
        try:
            item=os.stat(parts[-1],dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError:
            return None,None,None
        if not stat.S_ISREG(item.st_mode):
            return None,item,"special"
        flags=os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC
        filefd=os.open(parts[-1],flags,dir_fd=fd)
        return filefd,os.fstat(filefd),"file"
    finally:
        os.close(fd)
def inspect(path,version=False):
    try:
        fd,item,kind=safe_open(path)
    except FileNotFoundError:
        return {"kind":"missing"}
    except OSError as error:
        return {"kind":"unavailable"}
    if kind is None:
        return {"kind":"missing"}
    if kind!="file":
        return {"kind":"special"}
    try:
        if item.st_size<0 or item.st_size>MAX_FILE_BYTES:
            return {"kind":"oversize"}
        data=bytearray()
        while len(data)<=MAX_FILE_BYTES:
            chunk=os.read(fd,min(65536,MAX_FILE_BYTES+1-len(data)))
            if not chunk: break
            data.extend(chunk)
        if len(data)>MAX_FILE_BYTES:
            return {"kind":"oversize"}
        result={"kind":"file","sha256":hashlib.sha256(data).hexdigest(),
                "mode":stat.S_IMODE(item.st_mode),"uid":item.st_uid,"gid":item.st_gid}
        if version:
            try:
                parsed=json.loads(data.decode("utf-8"))
                version_value=parsed["version"]
                if (not isinstance(version_value,str) or len(version_value)>64
                    or any(ch not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz.+-" for ch in version_value)):
                    return {"kind":"invalid-version"}
                result={"kind":"version-json","mode":stat.S_IMODE(item.st_mode),
                        "uid":item.st_uid,"gid":item.st_gid,"version":version_value}
            except Exception: return {"kind":"invalid-version"}
        return result
    finally:
        os.close(fd)
def mount_id(fd):
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as source:
        for line in source:
            if line.startswith("mnt_id:"): return int(line.split()[1])
    raise OSError("mount identity unavailable")
def mount_info():
    try:
        if WORKSPACE != "/workspace":
            root_fd=os.open("/",os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            fd=root_fd
            try:
                for part in WORKSPACE.lstrip("/").split("/"):
                    nextfd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                    if fd!=root_fd: os.close(fd)
                    fd=nextfd
                info=os.fstat(fd)
                if info.st_uid!=1000 or stat.S_IMODE(info.st_mode)!=0o750:
                    return {"path":WORKSPACE,"fstype":"directory-mismatch","source":""}
                if info.st_dev!=os.fstat(root_fd).st_dev or mount_id(fd)!=mount_id(root_fd):
                    return {"path":WORKSPACE,"fstype":"different-filesystem","source":""}
                return {"path":WORKSPACE,"fstype":"disk-directory","source":"root"}
            finally:
                if fd!=root_fd: os.close(fd)
                os.close(root_fd)
        with open("/proc/self/mountinfo","rb") as source:
            data=source.read(65537)
        if len(data)>65536: return {"path":WORKSPACE,"fstype":"unavailable","source":""}
        lines=data.decode("ascii").splitlines()
        for line in lines:
            fields=line.split()
            sep=fields.index("-")
            target=fields[4].replace("\\040"," ").replace("\\011","\t").replace("\\134","\\")
            if target==WORKSPACE:
                return {"path":WORKSPACE,"fstype":fields[sep+1],"source":fields[sep+2]}
        return {"path":WORKSPACE,"fstype":"missing","source":""}
    except Exception:
        return {"path":WORKSPACE,"fstype":"unavailable","source":""}
inventory={}
for path in PATHS:
    inventory[path]=inspect(path,path==VERSION_PATH)
print(json.dumps({"schema":1,"inventory":inventory,"mount":mount_info()},
                 separators=(",",":"),sort_keys=True))
'''


def build_inspection_command(workspace_path: str = SYSTEM_WORKSPACE) -> str:
    """Build a constant, no-argument remote Python inspection command."""
    if workspace_path not in (SYSTEM_WORKSPACE, DEFAULT_WORKSPACE):
        raise ValueError("guest compatibility inspection accepts only fixed VM workspaces")
    expected = expected_inventory(workspace_path)
    paths = sorted(path for path, item in expected.items()
                   if item["kind"] not in ("mount", "disk-directory"))
    program = (_REMOTE_PROGRAM
               .replace("__PATHS__", repr(paths))
               .replace("__WORKSPACE__", repr(workspace_path))
               .replace("__VERSION_PATH__", repr(OPENCODE_PACKAGE_JSON)))
    return "/usr/bin/python3 -I -c " + shlex.quote(program)


def _duplicate_rejector(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def classify_inventory(stdout: bytes, expected: dict[str, dict], *,
                       max_bytes: int = MAX_RESPONSE_BYTES) -> tuple[str, tuple[str, ...]]:
    """Classify a fully parsed guest inventory; diagnostics contain fixed labels only."""
    if (not isinstance(stdout, bytes) or not isinstance(max_bytes, int)
            or max_bytes <= 0 or len(stdout) == 0 or len(stdout) > max_bytes):
        return "INCONCLUSIVE", ("response-size-or-type",)
    try:
        document = json.loads(stdout.decode("utf-8"), object_pairs_hook=_duplicate_rejector)
    except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return "INCONCLUSIVE", ("response-invalid",)
    if (not isinstance(document, dict) or set(document) != {"schema", "inventory", "mount"}
            or type(document.get("schema")) is not int or document["schema"] != 1
            or not isinstance(document.get("inventory"), dict)
            or not isinstance(document.get("mount"), dict)):
        return "INCONCLUSIVE", ("response-schema",)

    expected_assets = {
        path: item for path, item in expected.items()
        if item.get("kind") not in ("mount", "disk-directory")
    }
    observed = document["inventory"]
    expected_paths, observed_paths = set(expected_assets), set(observed)
    if observed_paths - expected_paths:
        return "INCONCLUSIVE", ("response-unexpected-path",)
    diagnostics: list[str] = []
    for index, path in enumerate(sorted(expected_paths), 1):
        if path not in observed:
            diagnostics.append(f"asset-{index}:missing")
            continue
        wanted, got = expected_assets[path], observed[path]
        if not isinstance(got, dict) or not isinstance(got.get("kind"), str):
            return "INCONCLUSIVE", ("asset-response-invalid",)
        if got["kind"] in ("unavailable", "oversize", "invalid-version"):
            return "INCONCLUSIVE", ("asset-observation-incomplete",)
        if got["kind"] in ("missing", "special"):
            diagnostics.append(f"asset-{index}:mismatch")
            continue
        required = ({"kind", "sha256", "mode", "uid", "gid"}
                    if wanted["kind"] == "file"
                    else {"kind", "mode", "uid", "gid", "version"})
        if set(got) != required:
            return "INCONCLUSIVE", ("asset-response-schema",)
        if (wanted["kind"] == "file"
                and (not isinstance(got.get("sha256"), str) or not _SHA256.fullmatch(got["sha256"]))):
            return "INCONCLUSIVE", ("asset-response-invalid",)
        if (type(got.get("mode")) is not int or type(got.get("uid")) is not int
                or type(got.get("gid")) is not int):
            return "INCONCLUSIVE", ("asset-response-invalid",)
        if (wanted["kind"] == "version-json" and not isinstance(got.get("version"), str)):
            return "INCONCLUSIVE", ("asset-response-invalid",)
        if any(got.get(key) != wanted.get(key) for key in required):
            diagnostics.append(f"asset-{index}:mismatch")

    mount = document["mount"]
    if set(mount) != {"path", "fstype", "source"} or not all(
            isinstance(mount.get(key), str) for key in ("path", "fstype", "source")):
        return "INCONCLUSIVE", ("mount-response-invalid",)
    workspace_path, expected_workspace = next(
        ((path, item) for path, item in expected.items()
         if item.get("kind") in ("mount", "disk-directory")), (None, None))
    if not isinstance(expected_workspace, dict):
        return "INCONCLUSIVE", ("candidate-workspace-contract-invalid",)
    if (mount.get("path") != workspace_path
            or any(mount.get(key) != expected_workspace.get(key) for key in ("fstype", "source"))):
        diagnostics.append("workspace-mount:mismatch")
    if diagnostics:
        return "MISMATCH", tuple(diagnostics[:32])
    return "MATCH", ()


def _evidence_number(value, *, maximum: int, mode: bool = False) -> str:
    if type(value) is not int or value < 0 or value > maximum:
        return "unknown"
    return f"{value:04o}" if mode else str(value)


def _evidence_version(value) -> str:
    if isinstance(value, str) and _SAFE_VERSION.fullmatch(value):
        return value
    return "unknown"


def _evidence_package(inventory: dict, wanted: dict) -> str:
    observed = inventory.get(OPENCODE_PACKAGE_JSON)
    if OPENCODE_PACKAGE_JSON not in inventory:
        return "present=unknown,regular=unknown,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=not-reported"
    if not isinstance(observed, dict) or not isinstance(observed.get("kind"), str):
        return "present=unknown,regular=unknown,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=invalid-observation"

    kind = observed["kind"]
    if kind == "missing":
        return "present=no,regular=no,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=missing"
    if kind == "special":
        return "present=yes,regular=no,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=special"
    if kind == "unavailable":
        return "present=unknown,regular=unknown,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=unavailable"
    if kind == "oversize":
        return "present=yes,regular=yes,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=oversize"
    if kind == "invalid-version":
        return "present=yes,regular=yes,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=invalid-version"
    if kind != "version-json" or set(observed) != {"kind", "mode", "uid", "gid", "version"}:
        return "present=unknown,regular=unknown,version=unknown,mode=unknown,uid=unknown,gid=unknown,category=invalid-observation"

    version = _evidence_version(observed["version"])
    mode = _evidence_number(observed["mode"], maximum=0o7777, mode=True)
    uid = _evidence_number(observed["uid"], maximum=0xFFFFFFFF)
    gid = _evidence_number(observed["gid"], maximum=0xFFFFFFFF)
    fields_valid = (version != "unknown" and mode != "unknown"
                    and uid != "unknown" and gid != "unknown")
    category = "invalid-observation" if not fields_valid else (
        "valid" if all(observed.get(key) == wanted.get(key)
                       for key in ("version", "mode", "uid", "gid")) else "mismatch")
    return (f"present=yes,regular=yes,version={version},mode={mode},uid={uid},gid={gid},"
            f"category={category}")


def _evidence_mount(mount: dict, wanted: dict,
                    workspace_path: str = SYSTEM_WORKSPACE) -> str:
    if not isinstance(mount, dict) or set(mount) != {"path", "fstype", "source"}:
        return "record=unknown,fstype=unknown,source=unknown,category=invalid-observation"

    path, fstype, source = mount["path"], mount["fstype"], mount["source"]
    if not all(isinstance(value, str) for value in (path, fstype, source)):
        return "record=unknown,fstype=unknown,source=unknown,category=invalid-observation"

    if fstype == "unavailable":
        return "record=unknown,fstype=unavailable,source=unknown,category=unavailable"
    if fstype == "missing":
        record = "no" if path == workspace_path else "unknown"
        source_label = "empty" if source == "" else "other"
        category = "missing" if record == "no" else "invalid-observation"
        return f"record={record},fstype=missing,source={source_label},category={category}"

    safe_fstype = fstype if _SAFE_FSTYPE.fullmatch(fstype) else "other"
    if source == wanted.get("source") == "workspace":
        safe_source = "workspace"
    elif source == "":
        safe_source = "empty"
    else:
        safe_source = "other"
    record = "yes" if path == workspace_path else "no"
    valid_fields = bool(_SAFE_FSTYPE.fullmatch(fstype))
    matches = (record == "yes" and valid_fields
               and fstype == wanted.get("fstype")
               and source == wanted.get("source"))
    category = "match" if matches else ("mismatch" if valid_fields else "invalid-observation")
    return f"record={record},fstype={safe_fstype},source={safe_source},category={category}"


def format_failure_evidence(stdout: bytes, expected: dict[str, dict], *,
                            cloud_init_ready_wait_ms: int | None = None,
                            workspace_path: str = SYSTEM_WORKSPACE) -> str:
    """Format a bounded, allowlisted summary; never include raw guest response data."""
    asset_paths = sorted(path for path, item in expected.items()
                         if item.get("kind") not in ("mount", "disk-directory"))
    try:
        asset_label = f"asset-{asset_paths.index(OPENCODE_PACKAGE_JSON) + 1}"
    except ValueError:
        asset_label = "asset-unknown"
    wanted_package = expected.get(OPENCODE_PACKAGE_JSON, {})
    expected_package = (
        f"present={'yes' if wanted_package.get('kind') == 'version-json' else 'unknown'},"
        f"regular={'yes' if wanted_package.get('kind') == 'version-json' else 'unknown'},"
        f"version={_evidence_version(wanted_package.get('version'))},"
        f"mode={_evidence_number(wanted_package.get('mode'), maximum=0o7777, mode=True)},"
        f"uid={_evidence_number(wanted_package.get('uid'), maximum=0xFFFFFFFF)},"
        f"gid={_evidence_number(wanted_package.get('gid'), maximum=0xFFFFFFFF)}")
    wanted_mount = expected.get(workspace_path, {})
    expected_mount = (
        f"record={'yes' if wanted_mount.get('kind') in ('mount', 'disk-directory') else 'unknown'},"
        f"fstype={wanted_mount.get('fstype') if isinstance(wanted_mount.get('fstype'), str) else 'unknown'},"
        f"source={wanted_mount.get('source') if isinstance(wanted_mount.get('source'), str) else 'unknown'}")

    response_category = ""
    document = None
    if not isinstance(stdout, bytes) or not stdout:
        response_category = "response-size-or-type"
    elif len(stdout) > MAX_RESPONSE_BYTES:
        response_category = "response-too-large"
    else:
        try:
            candidate = json.loads(stdout.decode("utf-8"), object_pairs_hook=_duplicate_rejector)
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
            response_category = "response-invalid"
        else:
            if (isinstance(candidate, dict) and candidate.get("schema") == 1
                    and type(candidate.get("schema")) is int
                    and isinstance(candidate.get("inventory"), dict)
                    and isinstance(candidate.get("mount"), dict)):
                document = candidate
            else:
                response_category = "response-schema"

    if document is None:
        observed_package = ("present=unknown,regular=unknown,version=unknown,mode=unknown,"
                            f"uid=unknown,gid=unknown,category={response_category}")
        observed_mount = ("record=unknown,fstype=unknown,source=unknown,"
                          f"category={response_category}")
    else:
        observed_package = _evidence_package(document["inventory"], wanted_package)
        observed_mount = _evidence_mount(document["mount"], wanted_mount, workspace_path)

    parts = [
        f"Guest compatibility evidence: {asset_label} expected{{{expected_package}}} "
        f"observed{{{observed_package}}}; workspace-mount expected{{{expected_mount}}} "
        f"observed{{{observed_mount}}}"
    ]
    if (type(cloud_init_ready_wait_ms) is int and 0 <= cloud_init_ready_wait_ms <= 900_000):
        parts.append(f"cloud-init-check=success readiness-wait-ms={cloud_init_ready_wait_ms}")
    line = "; ".join(parts)
    if len(line.encode("utf-8")) > MAX_FAILURE_EVIDENCE_BYTES:
        return "Guest compatibility evidence: unavailable (evidence-size-bound)"
    return line
