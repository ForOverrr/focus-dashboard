const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8090;
const ROOT = path.join(__dirname);
const DATA = path.join(ROOT, 'data');

const MIME = {
  '.html': 'text/html',
  '.css': 'text/css',
  '.js': 'application/javascript',
  '.json': 'application/json',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
};

function readJSON(file) {
  try { return JSON.parse(fs.readFileSync(path.join(DATA, file), 'utf8')); }
  catch { return file === 'sections.json' ? [] : []; }
}

function writeJSON(file, data) {
  fs.writeFileSync(path.join(DATA, file), JSON.stringify(data, null, 2) + '\n');
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', c => { body += c; if (body.length > 1e6) req.destroy(); });
    req.on('end', () => { try { resolve(JSON.parse(body)); } catch { reject(new Error('Invalid JSON')); } });
    req.on('error', reject);
  });
}

function sendJSON(res, code, data) {
  const json = JSON.stringify(data);
  res.writeHead(code, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
  res.end(json);
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') { res.writeHead(204); res.end(); return; }

  const urlPath = req.url.split('?')[0];

  // API routes

  // Feed refresh - runs fetch-feed.py in background
  if (urlPath === '/api/feed/refresh' && req.method === 'POST') {
    const { execFile } = require('child_process');
    sendJSON(res, 200, { status: 'started' });
    execFile('python3', ['fetch-feed.py'], { cwd: ROOT, timeout: 120000 }, (err) => {
      if (err) console.log('Feed refresh error:', err.message);
      else console.log('Feed refreshed');
    });
    return;
  }

  if (urlPath === '/api/todos' && req.method === 'POST') {
    try {
      const body = await parseBody(req);
      const todos = readJSON('todos.json');
      const sections = readJSON('sections.json');
      const maxId = todos.reduce((m, t) => Math.max(m, t.id || 0), 0);
      const newTodo = {
        id: maxId + 1,
        title: (body.title || '').trim(),
        priority: body.priority || 'medium',
        done: false,
        due: body.due || null,
        section: (body.section || 'else').toLowerCase(),
      };
      if (!newTodo.title) return sendJSON(res, 400, { error: 'Title required' });
      if (newTodo.section && !sections.includes(newTodo.section)) {
        sections.push(newTodo.section);
        writeJSON('sections.json', sections);
      }
      todos.push(newTodo);
      writeJSON('todos.json', todos);
      return sendJSON(res, 201, newTodo);
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  if (urlPath === '/api/notes' && req.method === 'POST') {
    try {
      const body = await parseBody(req);
      const notes = readJSON('notes.json');
      const maxId = notes.reduce((m, n) => Math.max(m, n.id || 0), 0);
      const newNote = {
        id: maxId + 1,
        text: (body.text || '').trim(),
        timestamp: new Date().toISOString(),
        tag: (body.tag || '').trim() || null,
        due: body.due || null,
      };
      if (!newNote.text) return sendJSON(res, 400, { error: 'Text required' });
      notes.push(newNote);
      writeJSON('notes.json', notes);
      return sendJSON(res, 201, newNote);
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  if (urlPath.startsWith('/api/notes/') && req.method === 'DELETE') {
    const id = parseInt(urlPath.split('/').pop(), 10);
    let notes = readJSON('notes.json');
    const len = notes.length;
    notes = notes.filter(n => n.id !== id);
    if (notes.length === len) return sendJSON(res, 404, { error: 'Not found' });
    writeJSON('notes.json', notes);
    return sendJSON(res, 200, { ok: true });
  }

  // --- Docs (Notes app) ---
  if (urlPath === '/api/docs' && req.method === 'GET') {
    return sendJSON(res, 200, readJSON('docs.json'));
  }

  if (urlPath === '/api/docs' && req.method === 'POST') {
    try {
      const body = await parseBody(req);
      const docs = readJSON('docs.json');
      const maxId = docs.reduce((m, d) => Math.max(m, d.id || 0), 0);
      const now = new Date().toISOString();
      const doc = {
        id: maxId + 1,
        title: (body.title || 'Untitled').trim(),
        body: (body.body || '').trim(),
        folder: (body.folder || 'General').trim(),
        pinned: false,
        created: now,
        updated: now,
      };
      docs.push(doc);
      writeJSON('docs.json', docs);
      return sendJSON(res, 201, doc);
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  if (urlPath.match(/^\/api\/docs\/\d+$/) && req.method === 'POST') {
    const id = parseInt(urlPath.split('/').pop(), 10);
    const docs = readJSON('docs.json');
    const doc = docs.find(d => d.id === id);
    if (!doc) return sendJSON(res, 404, { error: 'Not found' });
    try {
      const body = await parseBody(req);
      if (body.title !== undefined) doc.title = body.title;
      if (body.body !== undefined) doc.body = body.body;
      if (body.folder !== undefined) doc.folder = body.folder;
      if (body.pinned !== undefined) doc.pinned = body.pinned;
      doc.updated = new Date().toISOString();
      writeJSON('docs.json', docs);
      return sendJSON(res, 200, doc);
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  if (urlPath.match(/^\/api\/docs\/\d+$/) && req.method === 'DELETE') {
    const id = parseInt(urlPath.split('/').pop(), 10);
    let docs = readJSON('docs.json');
    const len = docs.length;
    docs = docs.filter(d => d.id !== id);
    if (docs.length === len) return sendJSON(res, 404, { error: 'Not found' });
    writeJSON('docs.json', docs);
    return sendJSON(res, 200, { ok: true });
  }

  if (urlPath.startsWith('/api/todos/') && req.method === 'POST') {
    const id = parseInt(urlPath.split('/').pop(), 10);
    const todos = readJSON('todos.json');
    const todo = todos.find(t => t.id === id);
    if (!todo) return sendJSON(res, 404, { error: 'Not found' });
    try {
      const body = await parseBody(req);
      if (body.done !== undefined) todo.done = body.done;
      if (body.title !== undefined) todo.title = body.title;
      if (body.priority !== undefined) todo.priority = body.priority;
      if (body.due !== undefined) todo.due = body.due;
      if (body.section !== undefined) todo.section = body.section;
      writeJSON('todos.json', todos);
      return sendJSON(res, 200, todo);
    } catch (e) { return sendJSON(res, 400, { error: e.message }); }
  }

  // Static files
  let filePath = path.join(ROOT, req.url.split('?')[0]);
  if (filePath.endsWith('/')) filePath = path.join(filePath, 'index.html');

  if (!filePath.startsWith(ROOT)) { res.writeHead(403); res.end(); return; }

  try {
    const stat = fs.statSync(filePath);
    if (stat.isDirectory()) filePath = path.join(filePath, 'index.html');
    const ext = path.extname(filePath);
    const mime = MIME[ext] || 'application/octet-stream';
    const data = fs.readFileSync(filePath);
    res.writeHead(200, { 'Content-Type': mime });
    res.end(data);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('Not found');
  }
});

server.listen(PORT, () => console.log(`Dashboard server on :${PORT}`));
