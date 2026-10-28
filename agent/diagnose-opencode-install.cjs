const fs = require("node:fs")
const path = require("node:path")
const { spawnSync } = require("node:child_process")
const { createRequire } = require("node:module")

const globalRoot = process.argv[2]
const cliRoot = path.join(globalRoot, "@opencode", "cli")
const cliPackage = path.join(cliRoot, "package.json")
const requireFromCli = createRequire(cliPackage)
const candidates = [
  "@opencode/cli-linux-x64-baseline",
  "@opencode/cli-linux-x64",
  "@opencode/cli-linux-x64-baseline-musl",
  "@opencode/cli-linux-x64-musl",
]

console.log("OpenCode platform-package diagnostics:")
console.log(`node=${process.version} platform=${process.platform} arch=${process.arch}`)
console.log(`glibc=${process.report?.getReport().header.glibcVersionRuntime ?? "not detected"}`)
console.log(`global_root=${globalRoot}`)
console.log(`cli_package_present=${fs.existsSync(cliPackage)}`)
try {
  const cpuInfo = fs.readFileSync("/proc/cpuinfo", "utf8")
  console.log(`cpu_avx2=${/(^|\s)avx2(\s|$)/im.test(cpuInfo)}`)
} catch (error) {
  console.log(`cpuinfo_error=${error.message}`)
}

for (const name of candidates) {
  const packageDir = path.join(cliRoot, "node_modules", ...name.split("/"))
  const packageJson = path.join(packageDir, "package.json")
  let metadata = "missing"
  if (fs.existsSync(packageJson)) {
    const pkg = JSON.parse(fs.readFileSync(packageJson, "utf8"))
    metadata = JSON.stringify({
      version: pkg.version,
      os: pkg.os,
      cpu: pkg.cpu,
      main: pkg.main,
      exports: pkg.exports,
      bin: pkg.bin,
    })
  }

  const files = []
  if (fs.existsSync(packageDir)) {
    const visit = (dir, depth = 0) => {
      if (depth > 2) return
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const fullPath = path.join(dir, entry.name)
        files.push(path.relative(packageDir, fullPath))
        if (entry.isDirectory()) visit(fullPath, depth + 1)
      }
    }
    visit(packageDir)
  }
  let packageJsonResolution
  try {
    packageJsonResolution = requireFromCli.resolve(`${name}/package.json`)
  } catch (error) {
    packageJsonResolution = `${error.code ?? error.name}: ${error.message.split("\n")[0]}`
  }
  console.log(`${name}: path=${packageDir}; package=${metadata}; package_json_resolve=${packageJsonResolution}; files=${JSON.stringify(files)}`)

  const binary = path.join(packageDir, "bin", "opencode")
  if (fs.existsSync(binary)) {
    const result = spawnSync(binary, ["--version"], { encoding: "utf8", timeout: 10000 })
    console.log(`${name}: binary_version_status=${result.status}; signal=${result.signal}; error=${result.error?.message ?? "none"}; stdout=${JSON.stringify(result.stdout?.slice(0, 500) ?? "")}; stderr=${JSON.stringify(result.stderr?.slice(0, 1000) ?? "")}`)
  }
}

const postinstall = path.join(cliRoot, "postinstall.mjs")
if (fs.existsSync(postinstall)) {
  const source = fs.readFileSync(postinstall, "utf8")
  console.log(`postinstall_source_begin (${source.length} bytes)`)
  console.log(source.slice(0, 16000))
  if (source.length > 16000) console.log("postinstall source truncated")
  console.log("postinstall_source_end")
}
