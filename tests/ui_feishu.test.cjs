const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ''; this.hidden = true; }
  append(...items) { this.children.push(...items); }
  setAttribute(key, value) { this[key] = value; }
  addEventListener() {}
}
const elements = new Map();
const get = (id) => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const text = (el) => el.textContent + el.children.map(text).join(' ');
const document = {getElementById: get, createElement: (tag) => new Element(tag)};
let status = {enabled: false};
const context = vm.createContext({document, console, Intl, Date, Map, Set,
  fetch: async () => ({ok: true, json: async () => status}),
  window: {CodeCrewAvatars: {create: () => new Element('img')}, setInterval: () => 1},
});
const root = path.join(__dirname, '../app/web');
vm.runInContext(fs.readFileSync(path.join(root, 'chat.js'), 'utf8').replace(/initializeChat\(\);\s*$/, ''), context);
vm.runInContext(`
  chatState.members.set('human', {role: 'human', name: 'human'});
  chatState.members.set('planner', {role: 'planner', name: '白金'});
  chatState.codingCapability.available = true;
  const externalA = {message_id: 'a', sender_id: 'human', content: '<script>not html</script>', correlation_id: 'ext', external_source: {external_sender_id: 'ou_a', display_name: '同名'}};
  const externalB = {...externalA, message_id: 'b', external_source: {external_sender_id: 'ou_b', display_name: '同名'}};
  const local = {...externalA, message_id: 'local', correlation_id: 'local', external_source: null};
  chatState.messages.set('a', {message: externalA});
  globalThis.a = renderMessage({message: externalA});
  globalThis.b = renderMessage({message: externalB});
  globalThis.local = renderMessage({message: local});
  globalThis.agent = renderMessage({message: {message_id:'agent', sender_id:'planner', correlation_id:'ext', content:'reply'}});
  globalThis.names = [chatSenderName(externalA), chatSenderName(externalB)];
  chooseCodingSource(externalA);
  globalThis.codingSource = chatState.codingSource;
`, context);
assert.notEqual(context.names[0], context.names[1]);
assert(text(context.a).includes('飞书 · 外部用户'));
assert(text(context.a).includes('<script>not html</script>'));
assert(!text(context.a).includes('受控编码'));
assert(!text(context.b).includes('受控编码'));
assert(text(context.local).includes('受控编码'));
assert.equal(context.codingSource, null);
assert(!text(context.agent).includes('回复这条消息'));

(async () => {
  vm.runInContext(fs.readFileSync(path.join(root, 'feishu.js'), 'utf8'), context);
  await vm.runInContext('refreshFeishuStatus()', context);
  assert.equal(get('feishu-status').hidden, true);
  status = {enabled:true, connection_state:'reconnecting', pending_outbox_count:2, retry_count:3, failed_count:1};
  await vm.runInContext('refreshFeishuStatus()', context);
  assert.equal(get('feishu-status').hidden, false);
  assert(get('feishu-status').textContent.includes('重连中'));
  assert(get('feishu-status').textContent.includes('待投递 2'));
  console.log('Feishu UI identity, authorization, reply isolation and status passed');
})().catch((error) => { console.error(error); process.exitCode = 1; });
