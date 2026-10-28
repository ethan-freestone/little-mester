const fs = require("node:fs")
const path = require("node:path")
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

for (const name of candidates) {
  const packageDir = path.join(cliRoot, "node_modules", ...name.split("/"))
  const packageJson = path.join(packageDir, "package.json")
  let metadata = "missing"
  if (fs.existsSync(packageJson)) {
    const pkg = JSON.parse(fs.readFileSync(packageJson, "utf8"))
    metadata = JSON.stringify({ version: pkg.version, os: pkg.os, cpu: pkg.cpu })
  }

  let resolution
  try {
    resolution = requireFromCli.resolve(name)
  } catch (error) {
    resolution = `${error.code ?? error.name}: ${error.message.split("\n")[0]}`
  }
  console.log(`${name}: package=${metadata}; resolve=${resolution}`)
}
