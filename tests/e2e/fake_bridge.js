// Stand-in for Hermes' Baileys WhatsApp bridge, for the end-to-end container test.
//
// Speaks the bridge's HTTP API (GET /health, GET /messages, POST /send, /send-media, /typing, /read,
// GET /chat/:id) so the real Hermes gateway and WhatsApp adapter run unchanged, plus test controls:
//   POST /inject     queue an inbound WhatsApp message (bridge event JSON; optional mediaFile)
//   POST /set-chat   {id, name}: the group subject /chat/:id reports
//   GET  /sent       everything Hermes sent, in order
// It also plays Gemini on port 3999: POST /v1beta/models/<model>:generateContent answers with the
// JSON in <dir>/gemini-response.json (or HTTP 500 when that file is missing).
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const args = process.argv.slice(2);
const opt = (name, dflt) => { const i = args.indexOf(name); return i >= 0 ? args[i + 1] : dflt; };
const PORT = parseInt(opt('--port', '3000'), 10);
const SESSION = path.resolve(opt('--session', '.'));
const DIR = __dirname;
const SENT_LOG = path.join(DIR, 'sent.jsonl');

const queue = [];
const chats = {};
let counter = 0;

function body(req) {
  return new Promise((resolve) => {
    let data = '';
    req.on('data', (c) => { data += c; });
    req.on('end', () => { try { resolve(data ? JSON.parse(data) : {}); } catch { resolve({}); } });
  });
}
function json(res, code, obj) { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(obj)); }
function record(kind, payload) {
  const entry = { kind, ...payload, at: new Date().toISOString() };
  fs.appendFileSync(SENT_LOG, JSON.stringify(entry) + '\n');
  return `SENT${++counter}`;
}

function cacheDirFor(kind) {
  if (kind === 'image' || kind === 'sticker') return process.env.HERMES_IMAGE_CACHE_DIR;
  if (kind === 'audio' || kind === 'ptt') return process.env.HERMES_AUDIO_CACHE_DIR;
  return process.env.HERMES_DOCUMENT_CACHE_DIR;
}

function inbound(msg) {
  const id = msg.messageId || `IN${Date.now()}${Math.floor(Math.random() * 1000)}`;
  const chatId = msg.chatId;
  const isGroup = chatId.endsWith('@g.us');
  const ev = {
    messageId: id, chatId, senderId: msg.senderId || chatId, senderName: msg.senderName || 'Someone',
    chatName: isGroup ? chatId.split('@')[0] : (msg.senderName || 'Someone'), isGroup,
    body: msg.body || '', hasMedia: false, mediaType: '', mime: '', fileName: '', nativeType: 'conversation',
    nativeMetadata: {}, mediaUrls: [], mentionedIds: [], quotedMessageId: null, quotedParticipant: null,
    quotedRemoteJid: null, quotedText: '', quotedMediaUrls: [], quotedMediaType: '', hasQuotedMessage: false,
    botIds: ['910000000000@s.whatsapp.net'], timestamp: Math.floor(Date.now() / 1000),
  };
  if (msg.mediaFile) {
    const { kind, src, mime, fileName } = msg.mediaFile;
    const dir = cacheDirFor(kind);
    fs.mkdirSync(dir, { recursive: true });
    const prefix = { image: 'img', video: 'vid', audio: 'aud', ptt: 'aud', document: 'doc' }[kind] || 'doc';
    const safe = fileName ? `_${path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_')}` : path.extname(src);
    const dest = path.join(dir, `${prefix}_${crypto.randomBytes(6).toString('hex')}${safe}`);
    fs.copyFileSync(src, dest);
    Object.assign(ev, { hasMedia: true, mediaType: kind, mime, fileName: fileName || '', mediaUrls: [dest],
      nativeType: `${kind}Message` });
    if (!ev.body) ev.body = `[${kind} received]`;
  }
  return ev;
}

http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x');
  if (req.method === 'GET' && url.pathname === '/health') {
    return json(res, 200, { status: 'connected', queueLength: queue.length, uptime: process.uptime(),
      scriptHash: '', sendReadReceipts: false, session: SESSION, capabilities: { outboundMentions: true } });
  }
  if (req.method === 'GET' && url.pathname === '/messages') return json(res, 200, queue.splice(0));
  if (req.method === 'GET' && url.pathname.startsWith('/chat/')) {
    const id = decodeURIComponent(url.pathname.slice(6));
    return json(res, 200, { name: chats[id] || id.replace(/@.*/, ''), isGroup: id.endsWith('@g.us'), participants: [] });
  }
  if (req.method === 'GET' && url.pathname === '/sent') {
    const lines = fs.existsSync(SENT_LOG) ? fs.readFileSync(SENT_LOG, 'utf8').split('\n').filter(Boolean) : [];
    return json(res, 200, lines.map((l) => JSON.parse(l)));
  }
  if (req.method !== 'POST') return json(res, 404, { error: 'not found' });
  const b = await body(req);
  switch (url.pathname) {
    case '/inject': queue.push(inbound(b)); return json(res, 200, { ok: true });
    case '/set-chat': chats[b.id] = b.name; return json(res, 200, { ok: true });
    case '/send': return json(res, 200, { success: true, messageId: record('text', { chatId: b.chatId, message: b.message }) });
    case '/send-media':
      if (!fs.existsSync(b.filePath)) return json(res, 500, { error: `missing ${b.filePath}` });
      return json(res, 200, { success: true, messageId: record('media', {
        chatId: b.chatId, mediaType: b.mediaType, caption: b.caption || '', fileName: b.fileName || '',
        file: path.basename(b.filePath) }) });
    case '/typing': case '/read': case '/edit': return json(res, 200, { success: true });
    default: return json(res, 404, { error: 'not found' });
  }
}).listen(PORT, '127.0.0.1', () => console.log(`fake bridge on ${PORT}, session ${SESSION}`));

http.createServer(async (req, res) => {
  await body(req);
  const file = path.join(DIR, 'gemini-response.json');
  fs.appendFileSync(path.join(DIR, 'gemini-calls.log'), `${new Date().toISOString()} ${req.url}\n`);
  if (!fs.existsSync(file)) return json(res, 500, { error: { message: 'fake gemini: no response configured' } });
  const text = fs.readFileSync(file, 'utf8');
  return json(res, 200, { candidates: [{ content: { parts: [{ text }] } }] });
}).listen(3999, '127.0.0.1');
