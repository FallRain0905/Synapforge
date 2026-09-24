// 阶段 7 协作验证：两个 Yjs 客户端并发编辑同一文档后必须收敛到同一内容。
// 直接使用 apps/web 安装的 yjs（与服务端中继配合的同一版本）。
import * as Y from "yjs";

function encodeBase64(bytes) {
  return Buffer.from(bytes).toString("base64");
}

function decodeBase64(value) {
  return new Uint8Array(Buffer.from(value, "base64"));
}

function relay(docA, docB) {
  // 模拟服务端中继：把一端的增量更新转发给另一端（base64 编解码与线上一致）。
  const updateA = encodeBase64(Y.encodeStateAsUpdate(docA));
  const updateB = encodeBase64(Y.encodeStateAsUpdate(docB));
  Y.applyUpdate(docA, decodeBase64(updateB), "remote");
  Y.applyUpdate(docB, decodeBase64(updateA), "remote");
}

const alice = new Y.Doc();
const bob = new Y.Doc();
const aliceText = alice.getText("document");
const bobText = bob.getText("document");

// 初始内容由任一端写入并同步。
aliceText.insert(0, "# 分析报告\n\n结论：模型可用。\n");
relay(alice, bob);

// 两人同时编辑不同位置（都没有先同步对方的这次编辑）。
aliceText.insert(aliceText.length, "Alice：补充灵敏度分析。\n");
bobText.insert(0, "Bob：补充数据来源。\n");

relay(alice, bob);
relay(alice, bob); // 二次同步确保双向收敛

const aliceResult = aliceText.toString();
const bobResult = bobText.toString();
const converged = aliceResult === bobResult;
const hasBothEdits = aliceResult.includes("Alice：补充灵敏度分析。") && aliceResult.includes("Bob：补充数据来源。");

// 并发修改同一段落也不应互相覆盖（CRDT 会保留两处插入）。
const carol = new Y.Doc();
const dave = new Y.Doc();
const carolText = carol.getText("document");
const daveText = dave.getText("document");
carolText.insert(0, "结论：成本下降 3%。");
relay(carol, dave);
carolText.insert(carolText.length, "（Carols 补充：口径为全周期）");
daveText.insert(daveText.length, "（Dave 补充：含折旧）");
relay(carol, dave);
relay(carol, dave);
const sameParagraphConverged = carolText.toString() === daveText.toString();
const keptBoth = carolText.toString().includes("Carols 补充") && carolText.toString().includes("Dave 补充");

console.log("alice:", JSON.stringify(aliceResult));
console.log("bob:  ", JSON.stringify(bobResult));
console.log("converged:", converged);
console.log("kept both edits:", hasBothEdits);
console.log("same-paragraph converged:", sameParagraphConverged, "| kept both:", keptBoth);

if (!converged || !hasBothEdits || !sameParagraphConverged || !keptBoth) {
  console.error("COLLAB_CONVERGENCE_FAILED");
  process.exit(1);
}
console.log("COLLAB_CONVERGENCE_OK");