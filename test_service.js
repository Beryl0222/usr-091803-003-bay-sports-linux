"use strict";

const { spawnSync } = require("node:child_process");

// discover 模式同时运行服务契约与领域场景测试
const result = spawnSync("python3", ["-m", "unittest", "discover", "-v", "-p", "test_*.py"], {
  stdio: "inherit",
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
