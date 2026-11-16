// 桌面壳的纯逻辑校验：版本比较 + latest.json 拉取。
// 用法：node scripts/check_studio_shell.js
const http = require("node:http");
const path = require("node:path");

const { isNewer } = require(path.join(__dirname, "..", "apps", "studio-desktop", "src", "version"));
const { fetchFeed } = require(path.join(__dirname, "..", "apps", "studio-desktop", "src", "feed"));

let pass = 0;
function ok(name) {
  pass += 1;
  console.log(`ok ${pass} - ${name}`);
}
function fail(name, error) {
  console.error(`FAIL - ${name}: ${error && error.message ? error.message : error}`);
  process.exitCode = 1;
}

async function main() {
  const versions = [
    ["0.2.0", "0.1.0", true],
    ["0.1.1", "0.1.0", true],
    ["0.1.0", "0.1.0", false],
    ["v1.0", "0.9.9", true],
    ["0.1.0-beta", "0.1.0", false],
    ["1.0.0", "0.9.9", true],
  ];
  for (const [candidate, current, expected] of versions) {
    if (isNewer(candidate, current) === expected) ok(`版本比较 ${candidate} > ${current}`);
    else fail(`版本比较 ${candidate} > ${current}`, "结果不符");
  }

  const server = http.createServer((request, response) => {
    if (request.url === "/ok") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ version: "0.2.0", url: "https://x/setup.exe", notes: "修了点东西" }));
    } else if (request.url === "/noversion") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ url: "https://x/setup.exe" }));
    } else {
      response.writeHead(404);
      response.end("no");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const feed = await fetchFeed(`${base}/ok`);
    if (feed.version === "0.2.0" && feed.url.endsWith("setup.exe") && feed.notes.includes("修了")) {
      ok("latest.json 正常解析");
    } else {
      fail("latest.json 正常解析", JSON.stringify(feed));
    }
    await fetchFeed(`${base}/noversion`).then(
      () => fail("缺 version 要报错", "没有报错"),
      (error) => (String(error.message).includes("version") ? ok("缺 version 要报错") : fail("缺 version 要报错", error)));
    await fetchFeed(`${base}/missing`).then(
      () => fail("404 要报错", "没有报错"),
      (error) => (String(error.message).includes("404") ? ok("404 要报错") : fail("404 要报错", error)));
  } finally {
    server.close();
  }

  if (!process.exitCode) console.log(`OK ${pass} 项全部通过`);
}

void main();
