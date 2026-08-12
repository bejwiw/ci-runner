import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { exec, spawn, execSync } from "child_process";
import express from "express";
import { z } from "zod";
import { 
  readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, 
  statSync, unlinkSync, rmdirSync, appendFileSync, createWriteStream
} from "fs";
import { resolve, extname, basename, dirname, join } from "path";
import { WebSocketServer } from "ws";
import pty from "node-pty";
import { launch } from "cloakbrowser";
import crypto from "crypto";

// ============ 配置管理 ============
const config = {
  // 服务器配置
  port: parseInt(process.env.MCP_PORT || "3457"),
  host: process.env.MCP_HOST || "0.0.0.0",
  
  // 文件服务配置
  filesDir: process.env.MCP_FILES_DIR || "/tmp/mcp-files",
  baseUrl: process.env.MCP_BASE_URL || "http://localhost:3457",
  
  // 终端配置
  terminalTimeoutMs: parseInt(process.env.MCP_TERMINAL_TIMEOUT_MS || "3600000"),
  terminalMaxSessions: parseInt(process.env.MCP_TERMINAL_MAX_SESSIONS || "50"),
  terminalCommandTimeout: parseInt(process.env.MCP_TERMINAL_CMD_TIMEOUT_MS || "120000"),
  terminalMaxOutputLength: parseInt(process.env.MCP_TERMINAL_MAX_OUTPUT || "100000"),
  
  // PTY配置
  ptyMaxSessions: parseInt(process.env.MCP_PTY_MAX_SESSIONS || "20"),
  ptyHeartbeatInterval: parseInt(process.env.MCP_PTY_HEARTBEAT_MS || "30000"),
  
  // 浏览器配置
  browserMaxSessions: parseInt(process.env.MCP_BROWSER_MAX_SESSIONS || "10"),
  browserTimeoutMs: parseInt(process.env.MCP_BROWSER_TIMEOUT_MS || "30000"),
  browserSessionTimeoutMs: parseInt(process.env.MCP_BROWSER_SESSION_TIMEOUT_MS || "1800000"),
  
  // 后台任务配置
  bgMaxTasks: parseInt(process.env.MCP_BG_MAX_TASKS || "50"),
  bgLogMaxSize: parseInt(process.env.MCP_BG_LOG_MAX_SIZE || "50000"),
  bgCleanupHours: parseInt(process.env.MCP_BG_CLEANUP_HOURS || "24"),
  
  // 搜索配置
  searchMaxRetries: parseInt(process.env.MCP_SEARCH_MAX_RETRIES || "3"),
  searchRetryDelays: [2000, 4000, 8000],
  searchMaxResults: parseInt(process.env.MCP_SEARCH_MAX_RESULTS || "20"),
  
  // 安全配置
  blockedPaths: (process.env.MCP_BLOCKED_PATHS || "/etc/shadow,/etc/passwd,/root/.ssh,/etc/ssh,/boot,/sys,/proc").split(","),
  allowedOrigins: (process.env.MCP_ALLOWED_ORIGINS || "*").split(","),
  
  // 日志配置
  logLevel: process.env.MCP_LOG_LEVEL || "info",
  logFile: process.env.MCP_LOG_FILE || "",
};

// 确保文件目录存在
if (!existsSync(config.filesDir)) {
  mkdirSync(config.filesDir, { recursive: true });
}

// ============ 日志系统 ============
const LOG_LEVELS = { debug: 0, info: 1, warn: 2, error: 3 };

function log(level, message, data = null) {
  const levelNum = LOG_LEVELS[level] ?? LOG_LEVELS.info;
  const minLevel = LOG_LEVELS[config.logLevel] ?? LOG_LEVELS.info;
  if (levelNum < minLevel) return;
  
  const timestamp = new Date().toISOString();
  const logEntry = `[${timestamp}] [${level.toUpperCase()}] ${message}${data ? ' ' + JSON.stringify(data) : ''}\n`;
  
  if (config.logFile) {
    try { appendFileSync(config.logFile, logEntry); } catch(e) {}
  }
  
  if (level === 'error' || level === 'warn') {
    console.error(logEntry.trim());
  } else {
    console.log(logEntry.trim());
  }
}

const logger = {
  debug: (msg, data) => log('debug', msg, data),
  info: (msg, data) => log('info', msg, data),
  warn: (msg, data) => log('warn', msg, data),
  error: (msg, data) => log('error', msg, data),
};

// ============ 工具函数 ============
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function formatDuration(ms) {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  return `${m}m${rs}s`;
}

function formatSize(bytes) {
  if (bytes > 1073741824) return `${(bytes / 1073741824).toFixed(2)}GB`;
  if (bytes > 1048576) return `${(bytes / 1048576).toFixed(1)}MB`;
  if (bytes > 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${bytes}B`;
}

function generateId(prefix = '') {
  return `${prefix}${Date.now()}_${crypto.randomBytes(4).toString('hex')}`;
}

function isPathSafe(targetPath, baseDir) {
  const resolved = resolve(targetPath);
  if (baseDir && !resolved.startsWith(resolve(baseDir))) {
    return false;
  }
  for (const blocked of config.blockedPaths) {
    if (resolved.startsWith(blocked)) {
      return false;
    }
  }
  return true;
}

// ============ MIME类型映射 ============
const MIME_MAP = {
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
  ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp", ".ico": "image/x-icon",
  ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
  ".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac", ".pcm": "audio/pcm",
  ".pdf": "application/pdf", ".doc": "application/msword", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".xls": "application/vnd.ms-excel", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".ppt": "application/vnd.ms-powerpoint", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".txt": "text/plain; charset=utf-8", ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".js": "application/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
  ".zip": "application/zip", ".tar": "application/x-tar", ".gz": "application/gzip", ".7z": "application/x-7z-compressed",
  ".xml": "application/xml", ".csv": "text/csv; charset=utf-8", ".log": "text/plain; charset=utf-8",
  ".sh": "text/x-shellscript; charset=utf-8", ".py": "text/x-python; charset=utf-8",
  ".md": "text/markdown; charset=utf-8", ".yaml": "text/yaml; charset=utf-8", ".yml": "text/yaml; charset=utf-8",
};

const INLINE_TYPES = new Set(["image", "audio", "video", "text"]);

function getMimeAndDisposition(filename) {
  const ext = extname(filename).toLowerCase();
  const mime = MIME_MAP[ext] || "application/octet-stream";
  const category = mime.split(";")[0].split("/")[0];
  return { mime, disposition: INLINE_TYPES.has(category) ? "inline" : "attachment", category };
}

function saveFileAndReturnUrl(buffer, prefix, ext) {
  const filename = `${prefix}_${Date.now()}_${crypto.randomBytes(4).toString('hex')}${ext}`;
  const filepath = resolve(config.filesDir, filename);
  writeFileSync(filepath, buffer);
  const { mime, disposition, category } = getMimeAndDisposition(filename);
  const url = `${config.baseUrl}/files/${filename}`;
  logger.info(`文件保存`, { filename, size: buffer.length, mime, category });
  return { filename, url, mime, disposition, category, size: buffer.length };
}

// ============ Express应用 ============
const app = express();
app.use(express.json({ limit: "100mb" }));
app.use(express.urlencoded({ extended: true, limit: "100mb" }));

// ============ CORS配置 ============
app.use((req, res, next) => {
  const origin = req.headers.origin || '*';
  if (config.allowedOrigins.includes('*') || config.allowedOrigins.includes(origin)) {
    res.setHeader("Access-Control-Allow-Origin", origin);
  }
  res.setHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS, PUT, DELETE");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept, X-Requested-With, X-Session-Id");
  res.setHeader("Access-Control-Max-Age", "86400");
  res.setHeader("Access-Control-Allow-Credentials", "true");
  if (req.method === "OPTIONS") return res.sendStatus(204);
  next();
});

// ============ 请求日志中间件 ============
app.use((req, res, next) => {
  logger.debug(`${req.method} ${req.url}`, { ip: req.ip, userAgent: req.headers['user-agent'] });
  next();
});

// ============ Token 认证 ============
const MCP_TOKEN = process.env.EXEC_TOKEN || "";
const PUBLIC_PATHS = new Set(["/health", "/"]);

app.use((req, res, next) => {
  if (PUBLIC_PATHS.has(req.path)) return next();
  const token = (req.headers["authorization"] || "").replace("Bearer ", "").trim()
    || (req.query.token || "").trim()
    || (req.body && req.body.token ? req.body.token : "");
  if (!MCP_TOKEN || token === MCP_TOKEN) return next();
  return res.status(401).json({ error: "未授权" });
});

// ============ 健康检查 ============
app.get("/health", (req, res) => {
  res.json({
    status: "ok",
    uptime: process.uptime(),
    memory: process.memoryUsage(),
    config: {
      port: config.port,
      filesDir: config.filesDir,
      terminalSessions: terminalSessions.size,
      ptySessions: ptySessions.size,
      browserSessions: browserSessions.size,
      bgTasks: bgTasks.size,
    }
  });
});

// ============ 持久化终端会话管理 ============
const terminalSessions = new Map();

class PersistentTerminal {
  constructor(sessionId, options = {}) {
    this.sessionId = sessionId;
    this.cwd = options.cwd || process.env.HOME || "/root";
    this.env = { ...process.env, ...(options.env || {}) };
    this.history = [];
    this.lastActiveAt = Date.now();
    this.createdAt = Date.now();
    this._process = null;
    this._busy = false;
    this._cmdQueue = [];
    this._spawn();
  }

  _spawn() {
    try {
      this._process = spawn("bash", ["--norc", "--noprofile"], {
        cwd: this.cwd,
        env: this.env,
        shell: false,
        stdio: ["pipe", "pipe", "pipe"],
      });
      this._process.stdin.write("set +m\n");
      this._process.on("error", (err) => {
        logger.error(`终端进程错误`, { sessionId: this.sessionId, error: err.message });
      });
      this._process.on("close", (code) => {
        logger.info(`终端进程退出`, { sessionId: this.sessionId, code });
        this._process = null;
      });
    } catch (err) {
      logger.error(`终端spawn失败`, { sessionId: this.sessionId, error: err.message });
      this._process = null;
    }
  }

  async _drainOutput(maxWaitMs = 1500) {
    return new Promise((resolve) => {
      if (!this._process) { resolve(); return; }
      let drained = "";
      const onData = (data) => { drained += data.toString(); };
      try {
        this._process.stdout.once("data", onData);
        this._process.stderr.once("data", onData);
        setTimeout(() => {
          try {
            this._process?.stdout?.removeListener("data", onData);
            this._process?.stderr?.removeListener("data", onData);
          } catch(e) {}
          if (drained) logger.debug(`排空残留输出`, { sessionId: this.sessionId, chars: drained.length });
          resolve();
        }, maxWaitMs);
      } catch(e) {
        resolve();
      }
    });
  }

  execute(command, timeout = config.terminalCommandTimeout) {
    return new Promise((resolve) => {
      if (this._busy) {
        this._cmdQueue.push({ command, timeout, resolve });
        logger.debug(`命令排队`, { sessionId: this.sessionId, queueDepth: this._cmdQueue.length, command: command.slice(0, 50) });
        return;
      }
      this._doExecute(command, timeout, resolve);
    });
  }

  _doExecute(command, timeout, resolve) {
    this._busy = true;
    if (!this._process || this._process.killed) {
      this._spawn();
      setTimeout(() => this._executeInner(command, timeout, resolve), 500);
      return;
    }
    this._executeInner(command, timeout, resolve);
  }

  _executeInner(command, timeout, resolve) {
    this.lastActiveAt = Date.now();
    this._busySince = Date.now();
    this.history.push({ cmd: command, ts: new Date().toISOString() });
    if (this.history.length > 200) this.history.shift();

    const MARKER = `__CMD_DONE_${Date.now()}_${crypto.randomBytes(4).toString('hex')}__`;
    const fullCmd = `${command}\necho "${MARKER}:\$?:\$(pwd)"\n`;

    let stdout = "";
    let stderr = "";
    let resolved = false;

    const onStdout = (data) => {
      stdout += data.toString();
      if (!resolved && stdout.includes(MARKER)) {
        resolved = true;
        cleanup();
        finishResult();
      }
    };
    const onStderr = (data) => { stderr += data.toString(); };

    try {
      this._process.stdout.on("data", onStdout);
      this._process.stderr.on("data", onStderr);
    } catch(e) {
      resolved = true;
      this._busy = false;
      this._processNextInQueue();
      resolve({ success: false, stdout: "", exit_code: -1, cwd: this.cwd, error: `监听失败: ${e.message}` });
      return;
    }

    const cleanup = () => {
      try {
        this._process?.stdout?.removeListener("data", onStdout);
        this._process?.stderr?.removeListener("data", onStderr);
      } catch (e) {}
      if (timer) clearTimeout(timer);
    };

    const finishResult = () => {
      const idx = stdout.lastIndexOf(MARKER);
      const afterMarker = stdout.substring(idx + MARKER.length);
      const markerLine = afterMarker.split("\n")[0] || "";
      const markerMatch = markerLine.match(/^:(\d+):(.+)/);

      let cleanOutput = stdout.substring(0, idx).trim();
      let cleanStderr = stderr.trim();

      let exitCode = 0;
      let detectedCwd = this.cwd;
      if (markerMatch) {
        exitCode = parseInt(markerMatch[1]) || 0;
        detectedCwd = markerMatch[2].trim();
        this.cwd = detectedCwd;
      }

      if (cleanStderr) {
        cleanOutput += (cleanOutput ? "\n" : "") + cleanStderr;
      }

      this._busy = false;
      this._processNextInQueue();

      resolve({
        success: exitCode === 0,
        stdout: cleanOutput || "",
        exit_code: exitCode,
        cwd: detectedCwd,
        timeout: false,
        warning: null,
      });
    };

    const timer = setTimeout(async () => {
      if (!resolved) {
        resolved = true;
        cleanup();

        let out = stdout.trim();
        if (stderr.trim()) out += (out ? "\n" : "") + stderr.trim();

        try { this._process?.stdin?.write("\x03"); } catch (e) {}
        await sleep(500);

        if (this._process && !this._process.killed) {
          try { this._process.kill("SIGKILL"); } catch (e) {}
          await sleep(300);
        }

        try {
          if (this._process && !this._process.killed) {
            this._process.kill("SIGKILL");
          }
        } catch(e) {}
        this._process = null;
        this._spawn();
        await sleep(500);

        this._busy = false;
        this._processNextInQueue();

        resolve({
          success: true,
          stdout: out || "(命令超时，无输出)",
          exit_code: -1,
          cwd: this.cwd,
          timeout: true,
          warning: `命令执行超过${formatDuration(timeout)}，已返回部分输出并强制终止进程`,
        });
      }
    }, Math.min(timeout, 1800000));

    try {
      this._process.stdin.write(fullCmd);
    } catch (e) {
      resolved = true;
      cleanup();
      this._busy = false;
      this._processNextInQueue();
      resolve({ success: false, stdout: "", exit_code: -1, cwd: this.cwd, error: `写入失败: ${e.message}` });
    }
  }

  _processNextInQueue() {
    if (this._cmdQueue.length === 0) return;
    const { command, timeout, resolve } = this._cmdQueue.shift();
    setTimeout(() => this._doExecute(command, timeout, resolve), 200);
  }

  getInfo() {
    return {
      session_id: this.sessionId,
      cwd: this.cwd,
      created_at: new Date(this.createdAt).toISOString(),
      last_active_at: new Date(this.lastActiveAt).toISOString(),
      idle_seconds: Math.floor((Date.now() - this.lastActiveAt) / 1000),
      history_count: this.history.length,
      recent_commands: this.history.slice(-5).map(h => h.cmd.slice(0, 100)),
      alive: !!(this._process && !this._process.killed),
      busy: this._busy,
      queue_depth: this._cmdQueue.length,
    };
  }

  destroy() {
    for (const item of this._cmdQueue) {
      item.resolve({ success: false, stdout: "", exit_code: -1, cwd: this.cwd, error: "会话已销毁" });
    }
    this._cmdQueue = [];
    this._busy = false;
    if (this._process) {
      try { this._process.kill("SIGTERM"); } catch (e) {}
      setTimeout(() => {
        try { this._process && this._process.kill("SIGKILL"); } catch (e) {}
      }, 2000);
    }
    terminalSessions.delete(this.sessionId);
    logger.info(`终端会话销毁`, { sessionId: this.sessionId });
  }
}

function getOrCreateTerminal(sessionId, options) {
  if (terminalSessions.has(sessionId)) {
    const term = terminalSessions.get(sessionId);
    term.lastActiveAt = Date.now();
    return term;
  }
  if (terminalSessions.size >= config.terminalMaxSessions) {
    let oldest = null, oldestTime = Infinity;
    for (const [sid, term] of terminalSessions) {
      if (term.lastActiveAt < oldestTime) { oldestTime = term.lastActiveAt; oldest = sid; }
    }
    if (oldest) { terminalSessions.get(oldest).destroy(); }
  }
  const term = new PersistentTerminal(sessionId, options);
  terminalSessions.set(sessionId, term);
  logger.info(`终端会话创建`, { sessionId, total: terminalSessions.size });
  return term;
}

// 定时清理过期终端会话
setInterval(() => {
  const now = Date.now();
  for (const [sid, term] of terminalSessions) {
    if (now - term.lastActiveAt > config.terminalTimeoutMs) {
      logger.info(`终端会话超时自动销毁`, { sessionId: sid });
      term.destroy();
    }
  }
}, 60000);

// ============ PTY会话管理（WebSocket终端） ============
let ptyAvailable = true;
try {
  if (!pty || typeof pty.spawn !== 'function') throw new Error('pty.spawn not available');
} catch (e) {
  ptyAvailable = false;
  logger.error('node-pty不可用，WebSocket终端功能被禁用');
}

const ptySessions = new Map();

class PtySession {
  constructor(sessionId, cols, rows) {
    this.sessionId = sessionId;
    this.cols = cols || 80;
    this.rows = rows || 24;
    this.createdAt = Date.now();
    this.lastActivity = this.createdAt;
    this.timeoutId = null;
    this.ptyProcess = null;
    this._dataCallbacks = [];
    this._onExit = null;
    this._exited = false;
    this._spawn();
  }

  _spawn() {
    if (!ptyAvailable) return;
    try {
      this.ptyProcess = pty.spawn('/bin/bash', ['-l'], {
        name: 'xterm-256color',
        cols: this.cols,
        rows: this.rows,
        cwd: process.env.HOME || '/root',
        env: { ...process.env, TERM: 'xterm-256color' }
      });
      this.ptyProcess.on('data', (data) => {
        this._touch();
        for (const cb of this._dataCallbacks) {
          try { cb(data); } catch(e) {}
        }
      });
      this.ptyProcess.on('exit', (code) => {
        logger.info(`PTY进程退出`, { sessionId: this.sessionId, code });
        this.ptyProcess = null;
        this._exited = true;
        this._exitCode = code;
        if (this._onExit) {
          try { this._onExit(code); } catch(e) {}
        }
      });
      this._resetTimeout();
      logger.info(`PTY会话创建`, { sessionId: this.sessionId, cols: this.cols, rows: this.rows });
    } catch (err) {
      logger.error(`PTY会话创建失败`, { sessionId: this.sessionId, error: err.message });
      this.ptyProcess = null;
    }
  }

  write(data) {
    if (!this.ptyProcess || this.ptyProcess.destroyed) return;
    try {
      this.ptyProcess.write(data);
      this._touch();
    } catch (err) {
      logger.error(`PTY写入失败`, { sessionId: this.sessionId, error: err.message });
    }
  }

  resize(cols, rows) {
    if (!this.ptyProcess || this.ptyProcess.destroyed) return;
    try {
      this.ptyProcess.resize(cols, rows);
      this.cols = cols;
      this.rows = rows;
      logger.debug(`PTY调整尺寸`, { sessionId: this.sessionId, cols, rows });
    } catch (err) {
      logger.error(`PTY调整尺寸失败`, { sessionId: this.sessionId, error: err.message });
    }
    this._touch();
  }

  onData(callback) {
    this._dataCallbacks.push(callback);
  }

  offData(callback) {
    const idx = this._dataCallbacks.indexOf(callback);
    if (idx !== -1) this._dataCallbacks.splice(idx, 1);
  }

  _touch() {
    this.lastActivity = Date.now();
    this._resetTimeout();
  }

  _resetTimeout() {
    if (this.timeoutId) clearTimeout(this.timeoutId);
    this.timeoutId = setTimeout(() => {
      logger.info(`PTY会话超时自动销毁`, { sessionId: this.sessionId });
      this.destroy();
    }, config.terminalTimeoutMs);
  }

  isAlive() {
    return this.ptyProcess && !this.ptyProcess.destroyed && !this._exited;
  }

  destroy() {
    if (this.timeoutId) clearTimeout(this.timeoutId);
    if (this.ptyProcess) {
      try { this.ptyProcess.kill(); } catch(e) {}
      this.ptyProcess = null;
    }
    ptySessions.delete(this.sessionId);
    logger.info(`PTY会话销毁`, { sessionId: this.sessionId });
  }
}

function getOrCreatePtySession(sessionId, cols, rows) {
  if (ptySessions.has(sessionId)) {
    const sess = ptySessions.get(sessionId);
    if (sess.isAlive()) {
      if (cols && rows && (sess.cols !== cols || sess.rows !== rows)) {
        sess.resize(cols, rows);
      }
      return sess;
    } else {
      ptySessions.delete(sessionId);
    }
  }
  if (ptySessions.size >= config.ptyMaxSessions) {
    let oldest = null, oldestTime = Infinity;
    for (const [sid, sess] of ptySessions) {
      if (sess.createdAt < oldestTime) {
        oldestTime = sess.createdAt;
        oldest = sid;
      }
    }
    if (oldest) {
      ptySessions.get(oldest).destroy();
    }
  }
  const sess = new PtySession(sessionId, cols, rows);
  ptySessions.set(sessionId, sess);
  return sess;
}

// ============ 浏览器自动化管理 ============
const browserSessions = new Map();

async function launchBrowser() {
  let browser = null;
  let retries = 3;
  while (retries > 0) {
    try {
      browser = await launch({
        headless: true,
        args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
      });
      const testCtx = await browser.newContext();
      const testPage = await testCtx.newPage();
      await testPage.goto("about:blank");
      await testPage.close();
      await testCtx.close();
      return browser;
    } catch (e) {
      logger.warn(`浏览器启动失败`, { retries: 4 - retries, error: e.message });
      if (browser) { try { await browser.close(); } catch(e2) {} }
      retries--;
      if (retries <= 0) throw e;
      await sleep(1000);
    }
  }
  throw new Error("浏览器启动失败");
}

async function getPage(sessName) {
  if (browserSessions.has(sessName)) {
    const sess = browserSessions.get(sessName);
    try {
      if (sess.browser && sess.browser.isConnected() && !sess.page.isClosed()) {
        sess.lastActiveAt = Date.now();
        return sess.page;
      }
    } catch (e) {
      logger.warn(`浏览器会话健康检查失败，将重建`, { session: sessName, error: e.message });
    }
    try { await sess.context.close(); } catch(e) {}
    try { await sess.browser.close(); } catch(e) {}
    browserSessions.delete(sessName);
  }

  let browser;
  try {
    browser = await launchBrowser();
  } catch (e) {
    logger.warn(`浏览器启动失败，清理所有旧会话后重试`, { error: e.message });
    for (const [name, sess] of browserSessions) {
      try { await sess.context.close(); } catch(e2) {}
      try { await sess.browser.close(); } catch(e2) {}
    }
    browserSessions.clear();
    browser = await launchBrowser();
  }

  const context = await browser.newContext({
    userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    viewport: { width: 1280, height: 720 },
    locale: "zh-CN",
    javaScriptEnabled: true,
    ignoreHTTPSErrors: true
  });
  const page = await context.newPage();
  browserSessions.set(sessName, { page, context, browser, createdAt: Date.now(), lastActiveAt: Date.now() });

  if (browserSessions.size > config.browserMaxSessions) {
    let oldest = null, oldestTime = Infinity;
    for (const [n, s] of browserSessions) {
      if (s.lastActiveAt < oldestTime) { oldestTime = s.lastActiveAt; oldest = n; }
    }
    if (oldest) {
      const oldSess = browserSessions.get(oldest);
      try { await oldSess.context.close(); } catch(e) {}
      try { await oldSess.browser.close(); } catch(e) {}
      browserSessions.delete(oldest);
    }
  }
  return page;
}

function cleanupBrowserSessions() {
  const now = Date.now();
  for (const [name, sess] of browserSessions) {
    let shouldClean = false;
    if (now - sess.lastActiveAt > config.browserSessionTimeoutMs) {
      shouldClean = true;
    }
    if (sess.browser && !sess.browser.isConnected()) {
      shouldClean = true;
    }
    if (sess.page && sess.page.isClosed()) {
      shouldClean = true;
    }
    if (shouldClean) {
      try { sess.context.close(); } catch(e) {}
      try { sess.browser.close(); } catch(e) {}
      browserSessions.delete(name);
      logger.info(`浏览器会话清理`, { session: name });
    }
  }
}
setInterval(cleanupBrowserSessions, 5 * 60 * 1000);

async function resolveSelector(page, selector) {
  if (!selector) return null;
  let locator = null;
  let desc = selector;
  try {
    if (selector.startsWith("role=")) {
      const parts = selector.split(",").reduce((acc, p) => {
        const [k, ...v] = p.split("=");
        acc[k.trim()] = v.join("=").trim();
        return acc;
      }, {});
      const roleName = parts.role;
      const opts = {};
      if (parts.name) opts.name = parts.name;
      if (parts.exact === "true") opts.exact = true;
      locator = page.getByRole(roleName, opts);
      desc = `getByRole(${roleName}${parts.name ? ", name=" + parts.name : ""})`;
    } else if (selector.startsWith("text=")) {
      const txt = selector.substring(5);
      locator = page.getByText(txt);
      desc = `getByText(${txt})`;
    } else if (selector.startsWith("label=")) {
      const lbl = selector.substring(6);
      locator = page.getByLabel(lbl);
      desc = `getByLabel(${lbl})`;
    } else if (selector.startsWith("placeholder=")) {
      const ph = selector.substring(12);
      locator = page.getByPlaceholder(ph);
      desc = `getByPlaceholder(${ph})`;
    } else if (selector.startsWith("testid=")) {
      const tid = selector.substring(7);
      locator = page.getByTestId(tid);
      desc = `getByTestId(${tid})`;
    } else if (selector.startsWith("title=")) {
      const t = selector.substring(6);
      locator = page.getByTitle(t);
      desc = `getByTitle(${t})`;
    } else if (selector.startsWith("alt=")) {
      const a = selector.substring(4);
      locator = page.getByAltText(a);
      desc = `getByAltText(${a})`;
    } else {
      locator = page.locator(selector);
      desc = `locator(${selector})`;
    }
  } catch (e) {
    logger.warn(`选择器解析失败`, { selector, error: e.message });
    locator = page.locator(selector);
    desc = selector;
  }
  return { locator, desc };
}

async function buildSnapshot(page) {
  const url = page.url();
  const title = await page.title();
  const elements = await page.evaluate(() => {
    const results = [];
    const interactive = document.querySelectorAll(
      "a, button, input, select, textarea, [role=button], [role=link], [role=tab], [onclick], [href], img, h1, h2, h3, h4, [data-testid], [aria-label], label, summary, details, [contenteditable]"
    );
    interactive.forEach((el) => {
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) return;
      const tag = el.tagName.toLowerCase();
      const text = (el.textContent || "").trim().substring(0, 120);
      const href = el.getAttribute("href") || "";
      const type = el.getAttribute("type") || "";
      const placeholder = el.getAttribute("placeholder") || "";
      const ariaLabel = el.getAttribute("aria-label") || "";
      const id = el.id || "";
      const cls = (el.className || "").substring?.(0, 80) || "";
      const src = el.getAttribute("src") || "";
      const alt = el.getAttribute("alt") || "";
      const value = el.getAttribute("value") || "";
      
      let selector = "";
      if (el.getAttribute("data-testid")) {
        selector = "testid=" + el.getAttribute("data-testid");
      } else if (id && /^[a-zA-Z][a-zA-Z0-9_-]*$/.test(id)) {
        selector = "#" + id;
      } else if (tag === "a" && text) {
        selector = "role=link,name=" + text.substring(0, 40);
      } else if (tag === "button" && text) {
        selector = "role=button,name=" + text.substring(0, 40);
      } else if (tag === "input" && type === "submit" && value) {
        selector = "role=button,name=" + value;
      } else if ((tag === "input" || tag === "textarea") && placeholder) {
        selector = "placeholder=" + placeholder;
      } else if ((tag === "input" || tag === "textarea") && ariaLabel) {
        selector = "label=" + ariaLabel;
      } else if (tag === "img" && alt) {
        selector = "alt=" + alt.substring(0, 40);
      } else if (el.getAttribute("title")) {
        selector = "title=" + el.getAttribute("title").substring(0, 40);
      } else if (text && text.length > 0 && text.length <= 40 && tag !== "input") {
        selector = "text=" + text;
      } else {
        selector = tag;
      }

      results.push({
        selector,
        tag,
        text: text || ariaLabel || placeholder || alt,
        href,
        type,
        placeholder,
        id,
        class: cls,
        src,
        value,
        editable: tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable,
        rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) }
      });
    });
    return results;
  });
  const bodyText = await page.evaluate(() => (document.body?.innerText || "").substring(0, 5000));
  return { url, title, elements, body_text: bodyText, element_count: elements.length };
}

// ============ 后台任务管理 ============
const bgTasks = new Map();

class BackgroundTask {
  constructor(options) {
    this.id = options.id || generateId('bg_');
    this.name = options.name || '';
    this.command = options.command;
    this.cwd = options.cwd || process.cwd();
    this.env = { ...process.env, ...(options.env || {}) };
    this.shell = options.shell || '/bin/bash';
    this.maxRuntime = parseInt(options.maxRuntime) || 0;
    this.status = 'running';
    this.exitCode = null;
    this.pid = null;
    this.createdAt = Date.now();
    this.startedAt = Date.now();
    this.finishedAt = null;
    this.log = '';
    this.logLength = 0;
    this.error = null;
    this._process = null;
    this._start();
  }

  _start() {
    try {
      this._process = spawn(this.shell, ['-c', this.command], {
        cwd: this.cwd,
        env: this.env,
        stdio: ['pipe', 'pipe', 'pipe'],
        detached: false,
      });
      this.pid = this._process.pid;

      this._process.stdout.on('data', (data) => this._appendLog(data));
      this._process.stderr.on('data', (data) => this._appendLog(data));

      this._process.on('error', (err) => {
        this.status = 'failed';
        this.error = `进程启动失败: ${err.message}`;
        this.finishedAt = Date.now();
        logger.error(`后台任务启动失败`, { taskId: this.id, error: err.message });
      });

      this._process.on('close', (code) => {
        this.exitCode = code;
        this.status = code === 0 ? 'completed' : 'failed';
        this.finishedAt = Date.now();
        logger.info(`后台任务完成`, { taskId: this.id, code });
      });

      if (this.maxRuntime > 0) {
        setTimeout(() => {
          if (this.status === 'running') {
            this._kill('SIGTERM');
            setTimeout(() => {
              if (this.status === 'running') {
                this._kill('SIGKILL');
                this.status = 'killed';
                this.finishedAt = Date.now();
                this.error = `任务运行超过${formatDuration(this.maxRuntime)}，已被强制终止`;
              }
            }, 3000);
          }
        }, this.maxRuntime);
      }
    } catch (err) {
      this.status = 'failed';
      this.error = `启动失败: ${err.message}`;
      this.finishedAt = Date.now();
      logger.error(`后台任务启动异常`, { taskId: this.id, error: err.message });
    }
  }

  _appendLog(data) {
    const text = data.toString();
    this.log += text;
    this.logLength += text.length;
    if (this.log.length > config.bgLogMaxSize) {
      this.log = this.log.slice(this.log.length - config.bgLogMaxSize);
      this.logLength = this.log.length;
    }
  }

  _kill(signal) {
    if (this._process && !this._process.killed) {
      try { this._process.kill(signal); } catch(e) {}
    }
  }

  getInfo() {
    return {
      task_id: this.id,
      name: this.name,
      command: this.command.length > 100 ? this.command.slice(0, 100) + '...' : this.command,
      cwd: this.cwd,
      status: this.status,
      pid: this.pid,
      exit_code: this.exitCode,
      created_at: new Date(this.createdAt).toISOString(),
      started_at: new Date(this.startedAt).toISOString(),
      finished_at: this.finishedAt ? new Date(this.finishedAt).toISOString() : null,
      duration_ms: (this.finishedAt || Date.now()) - this.startedAt,
      duration_human: formatDuration((this.finishedAt || Date.now()) - this.startedAt),
      log_size: this.log.length,
      error: this.error,
    };
  }

  destroy() {
    if (this.status === 'running') {
      this._kill('SIGTERM');
      setTimeout(() => {
        if (this.status === 'running') {
          this._kill('SIGKILL');
        }
      }, 3000);
      this.status = 'killed';
      this.finishedAt = Date.now();
      this.error = '被用户手动终止';
    }
    bgTasks.delete(this.id);
  }
}

function createBackgroundTask(options) {
  if (bgTasks.size >= config.bgMaxTasks) {
    let oldest = null, oldestTime = Infinity;
    for (const [id, task] of bgTasks) {
      if ((task.status === 'completed' || task.status === 'failed') && task.finishedAt < oldestTime) {
        oldestTime = task.finishedAt;
        oldest = id;
      }
    }
    if (oldest) {
      bgTasks.get(oldest).destroy();
    } else {
      throw new Error(`后台任务数量已达上限(${config.bgMaxTasks})，请先清理`);
    }
  }
  const task = new BackgroundTask(options);
  bgTasks.set(task.id, task);
  return task;
}

// 定期清理已完成的后台任务
setInterval(() => {
  const now = Date.now();
  const cleanupAfter = config.bgCleanupHours * 60 * 60 * 1000;
  for (const [id, task] of bgTasks) {
    if ((task.status === 'completed' || task.status === 'failed' || task.status === 'killed') &&
        task.finishedAt && (now - task.finishedAt > cleanupAfter)) {
      bgTasks.delete(id);
    }
  }
}, 60 * 60 * 1000);

// ============ 搜索服务（替代ZAI） ============
// 使用内置的fetch进行搜索，支持重试和超时
async function webSearch(query, num = 10, recencyDays = 30) {
  const maxRetries = config.searchMaxRetries;
  const delays = config.searchRetryDelays;
  const errors = [];
  
  for (let i = 1; i <= maxRetries; i++) {
    try {
      // 使用DuckDuckGo的API（免费，无需密钥）
      const url = `https://api.duckduckgo.com/?q=${encodeURIComponent(query)}&format=json&no_html=1&skip_disambig=1`;
      const response = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
        signal: AbortSignal.timeout(10000),
      });
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const data = await response.json();
      
      // 转换DuckDuckGo响应格式
      const results = [];
      if (data.RelatedTopics && data.RelatedTopics.length > 0) {
        for (const topic of data.RelatedTopics) {
          if (topic.Result) {
            // 从Result中提取标题和描述
            const match = topic.Result.match(/<a[^>]*>(.*?)<\/a>/);
            const title = match ? match[1] : topic.Text || '';
            const desc = topic.Text ? topic.Text.replace(/<[^>]+>/g, '').trim() : '';
            results.push({
              title: title || '无标题',
              url: topic.FirstURL || '',
              snippet: desc || '',
              host: topic.FirstURL ? new URL(topic.FirstURL).hostname : '',
            });
          }
        }
      }
      
      // 如果DuckDuckGo没有结果，尝试使用其他免费API
      if (results.length === 0) {
        // 使用Wikipedia作为备选
        const wikiUrl = `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=${encodeURIComponent(query)}&format=json&srlimit=${Math.min(num, 20)}`;
        const wikiResponse = await fetch(wikiUrl, {
          headers: { 'User-Agent': 'Mozilla/5.0' },
          signal: AbortSignal.timeout(8000),
        });
        if (wikiResponse.ok) {
          const wikiData = await wikiResponse.json();
          if (wikiData.query && wikiData.query.search) {
            for (const item of wikiData.query.search) {
              results.push({
                title: item.title,
                url: `https://en.wikipedia.org/wiki/${encodeURIComponent(item.title.replace(/ /g, '_'))}`,
                snippet: item.snippet ? item.snippet.replace(/<[^>]+>/g, '') : '',
                host: 'en.wikipedia.org',
              });
            }
          }
        }
      }
      
      return {
        success: true,
        data: results.slice(0, num),
        attempts: i,
      };
    } catch (err) {
      const errMsg = err?.message || String(err);
      errors.push({ attempt: i, error: errMsg, ts: new Date().toISOString() });
      logger.warn(`搜索失败`, { attempt: i, query, error: errMsg });
      if (i < maxRetries) await sleep(delays[i-1] || 2000);
    }
  }
  
  return {
    success: false,
    errors,
    attempts: maxRetries,
  };
}

async function pageReader(url, maxContentLength = 30000) {
  const maxRetries = config.searchMaxRetries;
  const delays = config.searchRetryDelays;
  
  for (let i = 1; i <= maxRetries; i++) {
    try {
      const response = await fetch(url, {
        headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36' },
        signal: AbortSignal.timeout(15000),
      });
      
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      
      const html = await response.text();
      
      // 提取标题
      const titleMatch = html.match(/<title[^>]*>([^<]*)<\/title>/i);
      const title = titleMatch ? titleMatch[1].trim() : '';
      
      // 提取文本内容
      let text = html
        .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
        .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      
      if (text.length > maxContentLength) {
        text = text.slice(0, maxContentLength) + '\n[截断]';
      }
      
      return {
        success: true,
        data: {
          title,
          html,
          text,
          html_length: html.length,
          text_length: text.length,
        },
        attempts: i,
      };
    } catch (err) {
      const errMsg = err?.message || String(err);
      logger.warn(`网页读取失败`, { attempt: i, url, error: errMsg });
      if (i < maxRetries) await sleep(delays[i-1] || 2000);
    }
  }
  
  return {
    success: false,
    errors: [{ attempt: maxRetries, error: '读取失败' }],
    attempts: maxRetries,
  };
}

// ============ MCP Server ============
function createServer() {
  const server = new McpServer({ name: "terminal", version: "8.0.0" });

  // 1. web_search
  server.tool("web_search", "联网搜索。支持detail参数展开完整内容。", {
    query: z.string().describe("搜索关键词"),
    num: z.number().min(1).max(20).default(10).describe("结果数"),
    recency_days: z.number().min(1).max(365).default(30).describe("时效天数"),
    detail: z.boolean().default(false).describe("是否获取每条结果的完整网页内容"),
    detail_max_length: z.number().min(500).max(30000).default(5000).describe("每条结果的文本最大字符数"),
  }, async ({ query, num, recency_days, detail, detail_max_length }) => {
    const t0 = Date.now();
    const result = await webSearch(query, num, recency_days);
    const ms = Date.now() - t0;
    
    if (result.success) {
      let results = result.data;
      
      if (detail && results.length > 0) {
        logger.info(`搜索详情: 并发读取${results.length}条结果的完整内容`);
        const detailPromises = results.map(async (item) => {
          if (!item.url) return { ...item, full_content: null, detail_error: "无URL" };
          try {
            const pr = await pageReader(item.url, detail_max_length);
            if (pr.success) {
              return {
                ...item,
                full_content: pr.data.text || '',
                content_length: pr.data.text_length || 0,
                detail_fetched: true,
                page_title: pr.data.title,
              };
            } else {
              return { ...item, full_content: null, detail_error: "读取失败" };
            }
          } catch(e) {
            return { ...item, full_content: null, detail_error: e?.message || String(e) };
          }
        });
        const detailResults = await Promise.all(detailPromises);
        const fetchedCount = detailResults.filter(r => r.detail_fetched).length;
        return {
          content: [{
            type: "text",
            text: JSON.stringify({
              success: true,
              query,
              result_count: results.length,
              detail_enabled: true,
              detail_fetched: fetchedCount,
              detail_failed: results.length - fetchedCount,
              duration_ms: ms,
              duration_human: formatDuration(ms),
              results: detailResults,
            })
          }]
        };
      }
      
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            success: true,
            query,
            result_count: results.length,
            detail_enabled: false,
            duration_ms: ms,
            duration_human: formatDuration(ms),
            results,
            tip: results.length > 0 && results.some(r => r.snippet && r.snippet.includes('...')) 
              ? '部分摘要被截断，设置detail=true可获取完整内容' 
              : undefined,
          })
        }]
      };
    }
    
    return {
      content: [{
        type: "text",
        text: JSON.stringify({
          success: false,
          query,
          error: "搜索失败",
          duration_ms: ms,
          duration_human: formatDuration(ms),
          attempts: result.attempts,
        })
      }]
    };
  });

  // 2. page_reader
  server.tool("page_reader", "读取网页完整内容。支持单个URL或多个URL批量访问。", {
    url: z.union([z.string(), z.array(z.string())]).describe("网页URL，单个URL字符串或URL数组"),
    max_content_length: z.number().min(500).max(30000).default(30000).describe("每条结果的文本最大字符数"),
  }, async ({ url, max_content_length }) => {
    const urls = Array.isArray(url) ? url : [url];
    const isBatch = urls.length > 1;
    const t0 = Date.now();

    async function readSingleUrl(singleUrl) {
      if (!singleUrl || typeof singleUrl !== "string") {
        return { url: String(singleUrl), success: false, error: "无效URL" };
      }
      const result = await pageReader(singleUrl, max_content_length);
      if (result.success) {
        return {
          url: singleUrl,
          success: true,
          title: result.data.title || "",
          text_content: result.data.text || "",
          content_length: result.data.text_length || 0,
          html_length: result.data.html_length || 0,
          attempts: result.attempts,
        };
      }
      return { url: singleUrl, success: false, error: "读取失败", attempts: result.attempts };
    }

    if (isBatch) {
      logger.info(`批量网页读取: ${urls.length}个URL`);
      const results = await Promise.all(urls.map(u => readSingleUrl(u)));
      const ms = Date.now() - t0;
      const successCount = results.filter(r => r.success).length;
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            success: true,
            batch_mode: true,
            total_urls: urls.length,
            success_count: successCount,
            failed_count: urls.length - successCount,
            duration_ms: ms,
            duration_human: formatDuration(ms),
            results,
          })
        }]
      };
    }

    const singleResult = await readSingleUrl(urls[0]);
    const ms = Date.now() - t0;
    return {
      content: [{
        type: "text",
        text: JSON.stringify({
          ...singleResult,
          duration_ms: ms,
          duration_human: formatDuration(ms),
        })
      }]
    };
  });

  // 3. file_upload
  server.tool("file_upload", "上传文件生成直链。", {
    file_base64: z.string().describe("文件base64"),
    filename: z.string().describe("文件名含扩展名"),
    content_type: z.string().optional().describe("可选MIME类型"),
  }, async ({ file_base64, filename, content_type }) => {
    try {
      if (!file_base64) {
        return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "file_base64不能为空" }) }] };
      }
      const buf = Buffer.from(file_base64, "base64");
      const ext = extname(filename).toLowerCase() || ".bin";
      const finalName = `${generateId('upload')}${ext}`;
      const filepath = resolve(config.filesDir, finalName);
      writeFileSync(filepath, buf);
      const mime = content_type || getMimeAndDisposition(finalName).mime;
      const { disposition, category } = getMimeAndDisposition(finalName);
      const url = `${config.baseUrl}/files/${finalName}`;
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            success: true,
            url,
            original_filename: filename,
            stored_filename: finalName,
            file_size_bytes: buf.length,
            file_size_human: formatSize(buf.length),
            content_type: mime,
            category,
            display_mode: disposition,
          })
        }]
      };
    } catch(e) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "上传失败: " + (e?.message || String(e)) }) }] };
    }
  });

  // 4. file_list
  server.tool("file_list", "列出/删除已上传文件。", {
    filter: z.enum(["all", "image", "video", "audio", "document", "other"]).default("all").describe("类型筛选"),
    action: z.enum(["list", "delete"]).default("list").describe("操作"),
    delete_filename: z.string().optional().describe("删除文件名"),
  }, async ({ filter, action, delete_filename }) => {
    if (action === "delete" && delete_filename) {
      const fp = resolve(config.filesDir, basename(delete_filename));
      if (!fp.startsWith(config.filesDir)) {
        return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "非法路径" }) }] };
      }
      try {
        if (existsSync(fp)) {
          unlinkSync(fp);
          return { content: [{ type: "text", text: JSON.stringify({ success: true, deleted: delete_filename }) }] };
        } else {
          return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "文件不存在" }) }] };
        }
      } catch(e) {
        return { content: [{ type: "text", text: JSON.stringify({ success: false, error: e?.message || String(e) }) }] };
      }
    }
    
    try {
      const files = readdirSync(config.filesDir)
        .map(name => {
          const fp = resolve(config.filesDir, name);
          try {
            const st = statSync(fp);
            const { mime, disposition, category } = getMimeAndDisposition(name);
            return {
              filename: name,
              url: `${config.baseUrl}/files/${name}`,
              size: st.size,
              size_human: formatSize(st.size),
              content_type: mime,
              category,
              display_mode: disposition,
              created: st.mtime.toISOString(),
            };
          } catch(e) {
            return null;
          }
        })
        .filter(Boolean)
        .sort((a, b) => new Date(b.created) - new Date(a.created));
      
      const filtered = filter === "all" ? files : files.filter(f => f.category === filter);
      const totalSize = files.reduce((s, f) => s + f.size, 0);
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            success: true,
            total_files: files.length,
            filtered_files: filtered.length,
            filter,
            total_size_bytes: totalSize,
            total_size_human: formatSize(totalSize),
            files: filtered,
          })
        }]
      };
    } catch(e) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, error: e?.message || String(e) }) }] };
    }
  });

  // 5. terminal_execute
  server.tool("terminal_execute", "在持久化终端中执行命令。", {
    command: z.string().describe("要执行的命令"),
    session_id: z.string().default("default").describe("终端会话ID"),
    timeout: z.number().min(1000).max(1800000).default(120000).describe("超时毫秒"),
    max_output_length: z.number().min(100).max(100000).default(10000).describe("输出最大字符数"),
  }, async ({ command, session_id, timeout, max_output_length }) => {
    const t0 = Date.now();
    try {
      if (!command || !command.trim()) {
        return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "命令不能为空" }) }] };
      }
      
      const term = getOrCreateTerminal(session_id);
      const result = await term.execute(command.trim(), timeout);
      const ms = Date.now() - t0;
      
      let stdout = result.stdout || "";
      if (stdout.length > max_output_length) {
        stdout = stdout.slice(0, max_output_length) + `\n[截断:原始${result.stdout.length}字符]`;
      }
      
      return {
        content: [{
          type: "text",
          text: JSON.stringify({
            success: result.success,
            stdout,
            exit_code: result.exit_code ?? 0,
            cwd: result.cwd || term.cwd,
            session_id,
            timeout: result.timeout || false,
            warning: result.warning || null,
            duration_ms: ms,
            duration_human: formatDuration(ms),
          })
        }]
      };
    } catch(e) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, error: e?.message || String(e) }) }] };
    }
  });

  // 6. browser_operation
  server.tool("browser_operation", "浏览器自动化操作。支持打开、点击、填写、截图等。", {
    action: z.enum(["open", "snapshot", "click", "fill", "type", "scroll", "hover", "screenshot", "back", "forward", "reload", "close", "execute", "wait", "resize", "select", "press", "upload", "drag", "extract", "pdf", "cookies"]).describe("操作类型"),
    url: z.string().optional().describe("URL（open时必填）"),
    selector: z.string().optional().describe("元素选择器（从snapshot获取）"),
    text: z.string().optional().describe("文本内容（fill/type/select/press时使用）"),
    session: z.string().default("default").describe("会话名称"),
    full_page: z.boolean().default(false).describe("是否全页截图"),
    wait_ms: z.number().min(100).max(30000).default(1000).describe("等待毫秒数"),
    timeout: z.number().min(3000).max(120000).default(30000).describe("超时毫秒"),
    execute_js: z.string().optional().describe("执行的JavaScript代码"),
    width: z.number().optional().describe("浏览器宽度"),
    height: z.number().optional().describe("浏览器高度"),
    wait_until: z.enum(["load", "domcontentloaded", "networkidle", "commit"]).default("domcontentloaded").describe("等待策略"),
    extract_selector: z.string().optional().describe("提取特定选择器内容"),
    file_path: z.string().optional().describe("上传文件路径"),
    drag_from: z.string().optional().describe("拖拽起始选择器"),
    drag_to: z.string().optional().describe("拖拽目标选择器"),
    keyboard: z.string().optional().describe("键盘操作: Enter|Tab|Escape等"),
    network_idle: z.boolean().default(false).describe("等待网络空闲"),
    screenshot_format: z.enum(["png", "jpeg"]).default("png").describe("截图格式"),
  }, async (params) => {
    try {
      const { action, url, selector, text, session, full_page, wait_ms, timeout, execute_js, width, height, wait_until, extract_selector, file_path, drag_from, drag_to, keyboard, network_idle, screenshot_format } = params;
      const sessName = session || "default";
      const timeoutMs = Math.min(Math.max(parseInt(timeout) || 30000, 3000), 120000);
      let result = {};
      
      switch (action) {
        case "open": {
          if (!url) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "open操作需要url参数" }) }] };
          const page = await getPage(sessName);
          await page.goto(url, { timeout: timeoutMs, waitUntil: wait_until });
          if (network_idle) await page.waitForLoadState("networkidle", { timeout: timeoutMs }).catch(() => {});
          result = { success: true, action: "open", url: page.url(), title: await page.title() };
          break;
        }
        case "snapshot": {
          const page = await getPage(sessName);
          result = { success: true, action: "snapshot", ...(await buildSnapshot(page)) };
          break;
        }
        case "click": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "click需要selector参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.click({ timeout: timeoutMs });
            await sleep(500);
          } catch (clickErr) {
            try {
              await sel.locator.first().click({ timeout: timeoutMs });
              await sleep(500);
            } catch (firstErr) {
              try {
                await sel.locator.first().click({ timeout: 5000, force: true });
                await sleep(500);
                result = { success: true, action: "click", target: sel.desc, method: "force_click" };
                break;
              } catch (forceErr) {
                return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `click失败: ${clickErr?.message || String(clickErr)}` }) }] };
              }
            }
          }
          result = { success: true, action: "click", target: sel.desc };
          break;
        }
        case "fill": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "fill需要selector参数" }) }] };
          if (text === undefined) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "fill需要text参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.fill(String(text), { timeout: 5000 });
          } catch (fillErr) {
            try {
              await sel.locator.click({ timeout: 3000 }).catch(() => {});
              await sleep(300);
              await sel.locator.fill(String(text), { timeout: 5000 });
            } catch (retryErr) {
              try {
                await sel.locator.click({ timeout: 3000 }).catch(() => {});
                await sleep(200);
                await sel.locator.pressSequentially(String(text), { delay: 30, timeout: 15000 });
                result = { success: true, action: "fill", target: sel.desc, filled: String(text), method: "type_fallback" };
                break;
              } catch (typeErr) {
                return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `fill失败: ${fillErr?.message || String(fillErr)}` }) }] };
              }
            }
          }
          result = { success: true, action: "fill", target: sel.desc, filled: String(text) };
          break;
        }
        case "type": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "type需要selector参数" }) }] };
          if (text === undefined) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "type需要text参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.click({ timeout: 5000 }).catch(() => {});
            await sleep(200);
            await sel.locator.pressSequentially(String(text), { delay: 50, timeout: timeoutMs });
          } catch (typeErr) {
            return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `type失败: ${typeErr?.message || String(typeErr)}` }) }] };
          }
          result = { success: true, action: "type", target: sel.desc, typed: String(text) };
          break;
        }
        case "scroll": {
          const page = await getPage(sessName);
          const direction = text || "down";
          const amounts = { down: [0, 500], up: [0, -500], left: [-500, 0], right: [500, 0], top: [0, -99999], bottom: [0, 99999] };
          const [dx, dy] = amounts[direction] || [0, 500];
          await page.mouse.wheel(dx, dy);
          await sleep(300);
          result = { success: true, action: "scroll", direction };
          break;
        }
        case "hover": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "hover需要selector参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.hover({ timeout: timeoutMs });
          } catch (hoverErr) {
            return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `hover失败: ${hoverErr?.message || String(hoverErr)}` }) }] };
          }
          result = { success: true, action: "hover", target: sel.desc };
          break;
        }
        case "screenshot": {
          const page = await getPage(sessName);
          const fmt = screenshot_format || "png";
          const buf = await page.screenshot({ fullPage: !!full_page, type: fmt });
          const fname = `screenshot_${Date.now()}.${fmt}`;
          const fpath = resolve(config.filesDir, fname);
          writeFileSync(fpath, buf);
          result = { success: true, action: "screenshot", file: fname, url: `${config.baseUrl}/files/${fname}`, size: formatSize(buf.length), full_page: !!full_page };
          break;
        }
        case "back": {
          const page = await getPage(sessName);
          await page.goBack({ timeout: timeoutMs, waitUntil: "domcontentloaded" }).catch(() => {});
          result = { success: true, action: "back", url: page.url() };
          break;
        }
        case "forward": {
          const page = await getPage(sessName);
          await page.goForward({ timeout: timeoutMs, waitUntil: "domcontentloaded" }).catch(() => {});
          result = { success: true, action: "forward", url: page.url() };
          break;
        }
        case "reload": {
          const page = await getPage(sessName);
          await page.reload({ timeout: timeoutMs, waitUntil: "domcontentloaded" });
          result = { success: true, action: "reload", url: page.url() };
          break;
        }
        case "close": {
          if (browserSessions.has(sessName)) {
            const sess = browserSessions.get(sessName);
            try { await sess.context.close(); } catch(e) {}
            try { await sess.browser?.close(); } catch(e) {}
            browserSessions.delete(sessName);
          }
          result = { success: true, action: "close", session: sessName };
          break;
        }
        case "execute": {
          if (!execute_js) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "execute需要execute_js参数" }) }] };
          const page = await getPage(sessName);
          const execResult = await page.evaluate(execute_js);
          result = { success: true, action: "execute", result: execResult };
          break;
        }
        case "wait": {
          const page = await getPage(sessName);
          const wm = Math.min(Math.max(parseInt(wait_ms) || 1000, 100), 30000);
          await page.waitForTimeout(wm);
          result = { success: true, action: "wait", waited_ms: wm };
          break;
        }
        case "resize": {
          const page = await getPage(sessName);
          const w = Math.min(Math.max(parseInt(width) || 1280, 320), 3840);
          const h = Math.min(Math.max(parseInt(height) || 720, 240), 2160);
          await page.setViewportSize({ width: w, height: h });
          result = { success: true, action: "resize", width: w, height: h };
          break;
        }
        case "select": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "select需要selector参数" }) }] };
          if (!text) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "select需要text参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.selectOption({ label: text }, { timeout: timeoutMs });
          } catch (selectErr) {
            try {
              await sel.locator.selectOption({ value: text }, { timeout: 5000 });
              result = { success: true, action: "select", target: sel.desc, selected: text, method: "value_match" };
              break;
            } catch (retryErr) {
              return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `select失败: ${selectErr?.message || String(selectErr)}` }) }] };
            }
          }
          result = { success: true, action: "select", target: sel.desc, selected: text };
          break;
        }
        case "press": {
          if (!text) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "press需要text参数" }) }] };
          const page = await getPage(sessName);
          await page.keyboard.press(text);
          result = { success: true, action: "press", key: text };
          break;
        }
        case "upload": {
          if (!selector) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "upload需要selector参数" }) }] };
          if (!file_path) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "upload需要file_path参数" }) }] };
          const page = await getPage(sessName);
          const sel = await resolveSelector(page, selector);
          if (!sel) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "无效的选择器" }) }] };
          try {
            await sel.locator.setInputFiles(file_path);
          } catch (uploadErr) {
            return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `upload失败: ${uploadErr?.message || String(uploadErr)}` }) }] };
          }
          result = { success: true, action: "upload", target: sel.desc, file: file_path };
          break;
        }
        case "drag": {
          if (!drag_from || !drag_to) {
            return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "drag需要drag_from和drag_to参数" }) }] };
          }
          const page = await getPage(sessName);
          const from = page.locator(drag_from);
          const to = page.locator(drag_to);
          await from.dragTo(to, { timeout: timeoutMs });
          result = { success: true, action: "drag", from: drag_from, to: drag_to };
          break;
        }
        case "extract": {
          const page = await getPage(sessName);
          const es = extract_selector || selector || "body";
          const content = await page.locator(es).first().textContent({ timeout: timeoutMs }).catch(() => null);
          const html = await page.locator(es).first().innerHTML({ timeout: timeoutMs }).catch(() => null);
          result = { success: true, action: "extract", selector: es, text: content?.trim() || null, html: html?.trim() || null };
          break;
        }
        case "pdf": {
          const page = await getPage(sessName);
          const buf = await page.pdf({ format: "A4", printBackground: true });
          const fname = `page_${Date.now()}.pdf`;
          const fpath = resolve(config.filesDir, fname);
          writeFileSync(fpath, buf);
          result = { success: true, action: "pdf", file: fname, url: `${config.baseUrl}/files/${fname}`, size: formatSize(buf.length) };
          break;
        }
        case "cookies": {
          const sess = browserSessions.get(sessName);
          if (!sess) return { content: [{ type: "text", text: JSON.stringify({ success: false, error: "会话不存在" }) }] };
          if (text) {
            const cookies = JSON.parse(text);
            await sess.context.addCookies(cookies);
            result = { success: true, action: "cookies", operation: "set" };
          } else {
            const cookies = await sess.context.cookies();
            result = { success: true, action: "cookies", operation: "get", cookies };
          }
          break;
        }
        default:
          return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `不支持的操作: ${action}` }) }] };
      }
      
      result.session = sessName;
      result.duration_ms = Date.now() - (result._startTime || Date.now());
      result.duration_human = formatDuration(result.duration_ms);
      return { content: [{ type: "text", text: JSON.stringify(result) }] };
    } catch(e) {
      logger.error(`浏览器操作失败`, { action: params.action, error: e.message });
      return { content: [{ type: "text", text: JSON.stringify({ success: false, error: e?.message || String(e) }) }] };
    }
  });

  return server;
}

// ============ API端点 ============

// 终端API
app.post("/api/terminal", async (req, res) => {
  const t0 = Date.now();
  try {
    const { command, session_id, timeout, cwd, env, max_output_length } = req.body || {};
    if (!command || typeof command !== "string" || !command.trim()) {
      return res.status(400).json({
        success: false,
        error: "缺少command参数",
        usage: {
          method: "POST /api/terminal",
          body: {
            command: "要执行的命令（必填）",
            session_id: "持久化终端ID（可选，默认default）",
            timeout: "超时ms（可选，默认120000，最大1800000）",
            cwd: "工作目录（可选）",
            env: "环境变量对象（可选）",
            max_output_length: "输出最大字符数（可选，默认10000）",
          },
        },
      });
    }

    const sid = session_id || "default";
    const cmdTimeout = Math.min(Math.max(parseInt(timeout) || config.terminalCommandTimeout, 1000), 1800000);
    const maxLen = Math.min(Math.max(parseInt(max_output_length) || 10000, 100), config.terminalMaxOutputLength);

    const term = getOrCreateTerminal(sid, { cwd, env });
    const result = await term.execute(command.trim(), cmdTimeout);
    const ms = Date.now() - t0;

    let stdout = result.stdout || "";
    if (stdout.length > maxLen) {
      stdout = stdout.slice(0, maxLen) + `\n[截断:原始${result.stdout.length}字符]`;
    }

    return res.json({
      success: result.success,
      stdout,
      exit_code: result.exit_code ?? 0,
      cwd: result.cwd || term.cwd,
      session_id: sid,
      timeout: result.timeout || false,
      warning: result.warning || null,
      duration_ms: ms,
      duration_human: formatDuration(ms),
    });
  } catch (e) {
    logger.error(`终端API异常`, { error: e.message });
    return res.status(500).json({ success: false, error: "服务器内部错误", detail: e?.message || String(e) });
  }
});

app.get("/api/terminal", async (req, res) => {
  const t0 = Date.now();
  try {
    const command = req.query.cmd || req.query.command || "";
    const sid = req.query.session || req.query.session_id || "default";
    const cmdTimeout = Math.min(Math.max(parseInt(req.query.timeout) || config.terminalCommandTimeout, 1000), 1800000);
    const maxLen = Math.min(Math.max(parseInt(req.query.max_output_length) || 10000, 100), config.terminalMaxOutputLength);

    if (!command.trim()) {
      return res.status(400).json({
        success: false,
        error: "缺少cmd参数",
        usage: "/api/terminal?cmd=命令&session=default&timeout=120000&max_output_length=10000",
      });
    }

    const term = getOrCreateTerminal(sid);
    const result = await term.execute(command.trim(), cmdTimeout);
    const ms = Date.now() - t0;

    let stdout = result.stdout || "";
    if (stdout.length > maxLen) {
      stdout = stdout.slice(0, maxLen) + `\n[截断:原始${result.stdout.length}字符]`;
    }

    return res.json({
      success: result.success,
      stdout,
      exit_code: result.exit_code ?? 0,
      cwd: result.cwd || term.cwd,
      session_id: sid,
      timeout: result.timeout || false,
      warning: result.warning || null,
      duration_ms: ms,
      duration_human: formatDuration(ms),
    });
  } catch (e) {
    logger.error(`终端API异常`, { error: e.message });
    return res.status(500).json({ success: false, error: "服务器内部错误", detail: e?.message || String(e) });
  }
});

app.get("/api/terminal/sessions", (req, res) => {
  try {
    const sessions = [];
    for (const [sid, term] of terminalSessions) {
      sessions.push(term.getInfo());
    }
    return res.json({
      success: true,
      total_sessions: sessions.length,
      max_sessions: config.terminalMaxSessions,
      sessions,
    });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

app.delete("/api/terminal/:sessionId", (req, res) => {
  try {
    const sid = req.params.sessionId;
    if (!terminalSessions.has(sid)) {
      return res.json({ success: false, error: `会话 ${sid} 不存在` });
    }
    terminalSessions.get(sid).destroy();
    return res.json({ success: true, message: `会话 ${sid} 已销毁` });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

// 搜索API
app.get("/api/search", async (req, res) => {
  const t0 = Date.now();
  try {
    const query = req.query.q || req.query.query || "";
    const num = Math.min(Math.max(parseInt(req.query.num || req.query.count || "10") || 10, 1), config.searchMaxResults);
    const recency_days = Math.min(Math.max(parseInt(req.query.recency_days || req.query.days || "30") || 30, 1), 365);
    const detail = req.query.detail === "true" || req.query.detail === "1";
    const detail_max_length = Math.min(Math.max(parseInt(req.query.detail_max_length || req.query.max_length || "5000") || 5000, 500), 30000);

    if (!query.trim()) {
      return res.status(400).json({
        success: false,
        error: "缺少搜索关键词",
        usage: "/api/search?q=关键词&num=10&recency_days=30&detail=true&detail_max_length=5000",
      });
    }

    logger.info(`API搜索`, { query, num, recency_days, detail });
    const result = await webSearch(query, num, recency_days);
    const searchMs = Date.now() - t0;

    if (result.success) {
      let results = result.data;

      if (detail && results.length > 0) {
        logger.info(`API搜索详情: 并发读取${results.length}条结果的完整内容`);
        const detailPromises = results.map(async (item) => {
          if (!item.url) return { ...item, full_content: null, detail_error: "无URL" };
          try {
            const pr = await pageReader(item.url, detail_max_length);
            if (pr.success) {
              return {
                ...item,
                full_content: pr.data.text || '',
                content_length: pr.data.text_length || 0,
                detail_fetched: true,
                page_title: pr.data.title,
              };
            } else {
              return { ...item, full_content: null, detail_error: "读取失败" };
            }
          } catch(e) {
            return { ...item, full_content: null, detail_error: e?.message || String(e) };
          }
        });
        const detailResults = await Promise.all(detailPromises);
        const totalMs = Date.now() - t0;
        const fetchedCount = detailResults.filter(r => r.detail_fetched).length;
        return res.json({
          success: true,
          query,
          result_count: results.length,
          num,
          recency_days,
          detail_enabled: true,
          detail_fetched: fetchedCount,
          detail_failed: results.length - fetchedCount,
          search_duration_ms: searchMs,
          search_duration_human: formatDuration(searchMs),
          total_duration_ms: totalMs,
          total_duration_human: formatDuration(totalMs),
          attempts: result.attempts,
          results: detailResults,
        });
      }

      return res.json({
        success: true,
        query,
        result_count: results.length,
        num,
        recency_days,
        attempts: result.attempts,
        duration_ms: searchMs,
        duration_human: formatDuration(searchMs),
        results,
        tip: results.length > 0 && results.some(r => r.snippet && r.snippet.includes('...'))
          ? '部分摘要被截断，添加 detail=true 可获取完整内容'
          : undefined,
      });
    }

    return res.status(503).json({
      success: false,
      query,
      error: "搜索服务暂时不可用",
      duration_ms: searchMs,
      duration_human: formatDuration(searchMs),
    });
  } catch (e) {
    logger.error(`API搜索异常`, { error: e.message });
    return res.status(500).json({ success: false, error: "服务器内部错误", detail: e?.message || String(e) });
  }
});

// 文件服务
app.get("/files/:filename", (req, res) => {
  try {
    const filename = basename(req.params.filename);
    const filepath = resolve(config.filesDir, filename);
    if (!filepath.startsWith(config.filesDir)) {
      return res.status(403).json({ error: "非法路径" });
    }
    if (!existsSync(filepath)) {
      return res.status(404).json({ error: "文件不存在" });
    }
    const { mime, disposition, category } = getMimeAndDisposition(filename);
    const st = statSync(filepath);
    res.setHeader("Content-Type", mime);
    res.setHeader("Content-Disposition", `${disposition}; filename="${filename}"`);
    res.setHeader("Content-Length", st.size);
    res.setHeader("Cache-Control", "public, max-age=86400");
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("X-File-Name", filename);
    res.setHeader("X-File-Size", st.size);
    if (category === "video") res.setHeader("Accept-Ranges", "bytes");
    res.send(readFileSync(filepath));
  } catch(e) {
    res.status(500).json({ error: e?.message || "文件读取失败" });
  }
});

// 文件下载
app.get("/api/file/download", (req, res) => {
  try {
    const filePath = req.query.path;
    if (!filePath || !filePath.trim()) {
      return res.status(400).json({ success: false, error: "缺少文件路径参数" });
    }
    const resolvedPath = resolve(filePath);
    if (!isPathSafe(resolvedPath)) {
      return res.status(403).json({ success: false, error: "禁止访问敏感文件或目录" });
    }
    if (!existsSync(resolvedPath)) {
      return res.status(404).json({ success: false, error: `文件不存在: ${resolvedPath}` });
    }
    const st = statSync(resolvedPath);
    if (st.isDirectory()) {
      try {
        const items = readdirSync(resolvedPath).map(name => {
          const itemPath = resolve(resolvedPath, name);
          try {
            const itemStat = statSync(itemPath);
            return {
              name,
              type: itemStat.isDirectory() ? "directory" : "file",
              size: itemStat.isDirectory() ? null : itemStat.size,
              size_human: itemStat.isDirectory() ? null : formatSize(itemStat.size),
              modified: itemStat.mtime.toISOString(),
              path: itemPath,
            };
          } catch (e) {
            return { name, type: "unknown", path: itemPath, error: e?.message };
          }
        }).sort((a, b) => {
          if (a.type === "directory" && b.type !== "directory") return -1;
          if (a.type !== "directory" && b.type === "directory") return 1;
          return a.name.localeCompare(b.name);
        });
        return res.json({
          success: true,
          type: "directory",
          path: resolvedPath,
          item_count: items.length,
          items,
        });
      } catch (e) {
        return res.status(500).json({ success: false, error: `无法读取目录: ${e?.message}` });
      }
    }

    const filename = basename(resolvedPath);
    const { mime, disposition, category } = getMimeAndDisposition(filename);
    res.setHeader("Content-Type", mime);
    res.setHeader("Content-Disposition", `${disposition}; filename="${filename}"`);
    res.setHeader("Content-Length", st.size);
    res.setHeader("Cache-Control", "no-cache, no-store, must-revalidate");
    res.setHeader("Access-Control-Allow-Origin", "*");
    res.setHeader("X-File-Name", filename);
    res.setHeader("X-File-Size", st.size);
    res.setHeader("X-File-Path", resolvedPath);
    if (category === "video" || st.size > 10 * 1024 * 1024) res.setHeader("Accept-Ranges", "bytes");
    logger.info(`文件下载`, { path: resolvedPath, size: st.size, mime });
    res.send(readFileSync(resolvedPath));
  } catch (e) {
    logger.error(`文件下载错误`, { error: e.message });
    return res.status(500).json({ success: false, error: e?.message || "文件下载失败" });
  }
});

// 文件上传
app.post("/api/file/upload", (req, res) => {
  try {
    const { path: targetPath, content_base64, content_text, create_dirs } = req.body;
    if (!targetPath || !targetPath.trim()) {
      return res.status(400).json({ success: false, error: "缺少目标路径参数" });
    }
    const resolvedPath = resolve(targetPath);
    if (!isPathSafe(resolvedPath)) {
      return res.status(403).json({ success: false, error: "禁止写入敏感目录" });
    }

    let fileBuffer;
    if (content_base64) {
      fileBuffer = Buffer.from(content_base64, "base64");
    } else if (content_text !== undefined) {
      fileBuffer = Buffer.from(content_text, "utf-8");
    } else {
      return res.status(400).json({ success: false, error: "缺少文件内容，请提供 content_base64 或 content_text" });
    }

    const dir = dirname(resolvedPath);
    if (create_dirs !== false && !existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
      logger.info(`自动创建目录`, { dir });
    }

    if (!existsSync(dir)) {
      return res.status(400).json({ success: false, error: `父目录不存在: ${dir}，可设置 create_dirs=true 自动创建` });
    }

    writeFileSync(resolvedPath, fileBuffer);
    const filename = basename(resolvedPath);
    const { mime } = getMimeAndDisposition(filename);
    const st = statSync(resolvedPath);
    logger.info(`文件上传`, { path: resolvedPath, size: st.size, mime });
    return res.json({
      success: true,
      path: resolvedPath,
      filename,
      size: st.size,
      size_human: formatSize(st.size),
      content_type: mime,
      created: st.mtime.toISOString(),
    });
  } catch (e) {
    logger.error(`文件上传错误`, { error: e.message });
    return res.status(500).json({ success: false, error: e?.message || "文件上传失败" });
  }
});

// 文件删除
app.delete("/api/file/delete", (req, res) => {
  try {
    const filePath = req.query.path;
    const recursive = req.query.recursive === "true" || req.query.recursive === "1";
    
    if (!filePath || !filePath.trim()) {
      return res.status(400).json({ success: false, error: "缺少文件路径参数" });
    }
    const resolvedPath = resolve(filePath);
    if (!isPathSafe(resolvedPath)) {
      return res.status(403).json({ success: false, error: "禁止删除系统敏感目录" });
    }
    if (resolvedPath === "/") {
      return res.status(403).json({ success: false, error: "禁止删除根目录" });
    }
    if (!existsSync(resolvedPath)) {
      return res.status(404).json({ success: false, error: `文件或目录不存在: ${resolvedPath}` });
    }

    const st = statSync(resolvedPath);
    if (st.isDirectory()) {
      if (!recursive) {
        return res.status(400).json({
          success: false,
          error: `路径是目录: ${resolvedPath}，请设置 recursive=true 以递归删除`,
        });
      }
      const deleteRecursive = (dirPath) => {
        const items = readdirSync(dirPath);
        for (const item of items) {
          const itemPath = resolve(dirPath, item);
          const itemStat = statSync(itemPath);
          if (itemStat.isDirectory()) {
            deleteRecursive(itemPath);
          } else {
            unlinkSync(itemPath);
          }
        }
        rmdirSync(dirPath);
      };
      deleteRecursive(resolvedPath);
      logger.info(`目录递归删除`, { path: resolvedPath });
      return res.json({
        success: true,
        deleted: resolvedPath,
        recursive: true,
        message: `目录已递归删除: ${resolvedPath}`,
      });
    } else {
      unlinkSync(resolvedPath);
      logger.info(`文件删除`, { path: resolvedPath });
      return res.json({
        success: true,
        deleted: resolvedPath,
        size: st.size,
        size_human: formatSize(st.size),
      });
    }
  } catch (e) {
    logger.error(`文件删除错误`, { error: e.message });
    return res.status(500).json({ success: false, error: e?.message || "文件删除失败" });
  }
});

// 后台任务API
app.post("/api/background", async (req, res) => {
  try {
    const { command, cwd, env, timeout, max_runtime, name, shell } = req.body || {};
    
    if (!command || typeof command !== "string" || !command.trim()) {
      return res.status(400).json({
        success: false,
        error: "缺少command参数",
        usage: {
          method: "POST /api/background",
          body: {
            command: "要执行的命令（必填）",
            cwd: "工作目录（可选）",
            env: "环境变量对象（可选）",
            timeout: "命令启动超时ms（可选，默认10000）",
            max_runtime: "最大运行时间ms（可选，默认0不限制）",
            name: "任务名称（可选）",
            shell: "使用的shell（可选，默认/bin/bash）",
          },
        },
      });
    }

    const task = createBackgroundTask({
      command: command.trim(),
      cwd: cwd || process.cwd(),
      env: env || {},
      maxRuntime: parseInt(max_runtime) || 0,
      name: name || "",
      shell: shell || "/bin/bash",
    });

    return res.json({
      success: true,
      task_id: task.id,
      pid: task.pid,
      command: command.trim(),
      cwd: task.cwd,
      status: "running",
      created_at: new Date(task.createdAt).toISOString(),
      max_runtime_ms: task.maxRuntime || null,
      max_runtime_human: task.maxRuntime ? formatDuration(task.maxRuntime) : "无限制",
    });
  } catch (e) {
    logger.error(`后台任务创建异常`, { error: e.message });
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

app.get("/api/background", (req, res) => {
  try {
    const { status, limit, offset } = req.query;
    let tasks = [];
    for (const [id, task] of bgTasks) {
      if (status && task.status !== status) continue;
      tasks.push(task.getInfo());
    }
    tasks.sort((a, b) => b.created_at.localeCompare(a.created_at));
    const off = parseInt(offset) || 0;
    const lim = Math.min(parseInt(limit) || 50, 100);
    tasks = tasks.slice(off, off + lim);
    return res.json({
      success: true,
      total_tasks: bgTasks.size,
      filtered_count: tasks.length,
      limit: lim,
      offset: off,
      tasks,
    });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

app.get("/api/background/:taskId", (req, res) => {
  try {
    const { taskId } = req.params;
    const task = bgTasks.get(taskId);
    if (!task) {
      return res.status(404).json({ success: false, error: `任务 ${taskId} 不存在` });
    }
    const { tail, head } = req.query;
    let log = task.log;
    if (tail) {
      const n = Math.min(parseInt(tail) || 1000, 50000);
      log = log.length > n ? log.slice(log.length - n) : log;
    } else if (head) {
      const n = Math.min(parseInt(head) || 1000, 50000);
      log = log.length > n ? log.slice(0, n) : log;
    }
    return res.json({
      success: true,
      ...task.getInfo(),
      log,
    });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

app.delete("/api/background/:taskId", (req, res) => {
  try {
    const { taskId } = req.params;
    const task = bgTasks.get(taskId);
    if (!task) {
      return res.status(404).json({ success: false, error: `任务 ${taskId} 不存在` });
    }
    task.destroy();
    return res.json({ success: true, message: `任务 ${taskId} 已终止并删除` });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

// 浏览器API
app.post("/api/browser", async (req, res) => {
  const t0 = Date.now();
  try {
    const { action, url, selector, text, session, full_page, wait_ms, timeout, execute_js, width, height, wait_until, extract_selector, file_path, drag_from, drag_to, keyboard, network_idle, screenshot_format } = req.body || {};
    
    if (!action || typeof action !== "string") {
      return res.status(400).json({
        success: false,
        error: "缺少action参数",
        supported_actions: ["open", "snapshot", "click", "fill", "type", "scroll", "hover", "screenshot", "back", "forward", "reload", "close", "execute", "wait", "resize", "select", "press", "upload", "drag", "extract", "pdf", "cookies"],
      });
    }

    const sessName = session || "default";
    const timeoutMs = Math.min(Math.max(parseInt(timeout) || config.browserTimeoutMs, 3000), 120000);
    let result = {};

    switch (action) {
      case "open": {
        if (!url) return res.status(400).json({ success: false, error: "open操作需要url参数" });
        const page = await getPage(sessName);
        await page.goto(url, { timeout: timeoutMs, waitUntil: wait_until || "domcontentloaded" });
        if (network_idle) await page.waitForLoadState("networkidle", { timeout: timeoutMs }).catch(() => {});
        result = { success: true, action: "open", url: page.url(), title: await page.title() };
        break;
      }
      case "snapshot": {
        const page = await getPage(sessName);
        result = { success: true, action: "snapshot", ...(await buildSnapshot(page)) };
        break;
      }
      case "click": {
        if (!selector) return res.status(400).json({ success: false, error: "click需要selector参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.click({ timeout: timeoutMs });
          await sleep(500);
        } catch (clickErr) {
          try {
            await sel.locator.first().click({ timeout: timeoutMs });
            await sleep(500);
          } catch (firstErr) {
            try {
              await sel.locator.first().click({ timeout: 5000, force: true });
              await sleep(500);
              result = { success: true, action: "click", target: sel.desc, method: "force_click" };
              break;
            } catch (forceErr) {
              return res.status(500).json({ success: false, error: `click失败: ${clickErr?.message || String(clickErr)}` });
            }
          }
        }
        result = { success: true, action: "click", target: sel.desc };
        break;
      }
      case "fill": {
        if (!selector) return res.status(400).json({ success: false, error: "fill需要selector参数" });
        if (text === undefined) return res.status(400).json({ success: false, error: "fill需要text参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.fill(String(text), { timeout: 5000 });
        } catch (fillErr) {
          try {
            await sel.locator.click({ timeout: 3000 }).catch(() => {});
            await sleep(300);
            await sel.locator.fill(String(text), { timeout: 5000 });
          } catch (retryErr) {
            try {
              await sel.locator.click({ timeout: 3000 }).catch(() => {});
              await sleep(200);
              await sel.locator.pressSequentially(String(text), { delay: 30, timeout: 15000 });
              result = { success: true, action: "fill", target: sel.desc, filled: String(text), method: "type_fallback" };
              break;
            } catch (typeErr) {
              return res.status(500).json({ success: false, error: `fill失败: ${fillErr?.message || String(fillErr)}` });
            }
          }
        }
        result = { success: true, action: "fill", target: sel.desc, filled: String(text) };
        break;
      }
      case "type": {
        if (!selector) return res.status(400).json({ success: false, error: "type需要selector参数" });
        if (text === undefined) return res.status(400).json({ success: false, error: "type需要text参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.click({ timeout: 5000 }).catch(() => {});
          await sleep(200);
          await sel.locator.pressSequentially(String(text), { delay: 50, timeout: timeoutMs });
        } catch (typeErr) {
          return res.status(500).json({ success: false, error: `type失败: ${typeErr?.message || String(typeErr)}` });
        }
        result = { success: true, action: "type", target: sel.desc, typed: String(text) };
        break;
      }
      case "scroll": {
        const page = await getPage(sessName);
        const direction = text || "down";
        const amounts = { down: [0, 500], up: [0, -500], left: [-500, 0], right: [500, 0], top: [0, -99999], bottom: [0, 99999] };
        const [dx, dy] = amounts[direction] || [0, 500];
        await page.mouse.wheel(dx, dy);
        await sleep(300);
        result = { success: true, action: "scroll", direction };
        break;
      }
      case "hover": {
        if (!selector) return res.status(400).json({ success: false, error: "hover需要selector参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.hover({ timeout: timeoutMs });
        } catch (hoverErr) {
          return res.status(500).json({ success: false, error: `hover失败: ${hoverErr?.message || String(hoverErr)}` });
        }
        result = { success: true, action: "hover", target: sel.desc };
        break;
      }
      case "screenshot": {
        const page = await getPage(sessName);
        const fmt = screenshot_format || "png";
        const buf = await page.screenshot({ fullPage: !!full_page, type: fmt });
        const fname = `screenshot_${Date.now()}.${fmt}`;
        const fpath = resolve(config.filesDir, fname);
        writeFileSync(fpath, buf);
        result = { success: true, action: "screenshot", file: fname, url: `${config.baseUrl}/files/${fname}`, size: formatSize(buf.length), full_page: !!full_page };
        break;
      }
      case "back": {
        const page = await getPage(sessName);
        await page.goBack({ timeout: timeoutMs, waitUntil: "domcontentloaded" }).catch(() => {});
        result = { success: true, action: "back", url: page.url() };
        break;
      }
      case "forward": {
        const page = await getPage(sessName);
        await page.goForward({ timeout: timeoutMs, waitUntil: "domcontentloaded" }).catch(() => {});
        result = { success: true, action: "forward", url: page.url() };
        break;
      }
      case "reload": {
        const page = await getPage(sessName);
        await page.reload({ timeout: timeoutMs, waitUntil: "domcontentloaded" });
        result = { success: true, action: "reload", url: page.url() };
        break;
      }
      case "close": {
        if (browserSessions.has(sessName)) {
          const sess = browserSessions.get(sessName);
          try { await sess.context.close(); } catch(e) {}
          try { await sess.browser?.close(); } catch(e) {}
          browserSessions.delete(sessName);
        }
        result = { success: true, action: "close", session: sessName };
        break;
      }
      case "execute": {
        if (!execute_js) return res.status(400).json({ success: false, error: "execute需要execute_js参数" });
        const page = await getPage(sessName);
        const execResult = await page.evaluate(execute_js);
        result = { success: true, action: "execute", result: execResult };
        break;
      }
      case "wait": {
        const page = await getPage(sessName);
        const wm = Math.min(Math.max(parseInt(wait_ms) || 1000, 100), 30000);
        await page.waitForTimeout(wm);
        result = { success: true, action: "wait", waited_ms: wm };
        break;
      }
      case "resize": {
        const page = await getPage(sessName);
        const w = Math.min(Math.max(parseInt(width) || 1280, 320), 3840);
        const h = Math.min(Math.max(parseInt(height) || 720, 240), 2160);
        await page.setViewportSize({ width: w, height: h });
        result = { success: true, action: "resize", width: w, height: h };
        break;
      }
      case "select": {
        if (!selector) return res.status(400).json({ success: false, error: "select需要selector参数" });
        if (!text) return res.status(400).json({ success: false, error: "select需要text参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.selectOption({ label: text }, { timeout: timeoutMs });
        } catch (selectErr) {
          try {
            await sel.locator.selectOption({ value: text }, { timeout: 5000 });
            result = { success: true, action: "select", target: sel.desc, selected: text, method: "value_match" };
            break;
          } catch (retryErr) {
            return res.status(500).json({ success: false, error: `select失败: ${selectErr?.message || String(selectErr)}` });
          }
        }
        result = { success: true, action: "select", target: sel.desc, selected: text };
        break;
      }
      case "press": {
        if (!text) return res.status(400).json({ success: false, error: "press需要text参数" });
        const page = await getPage(sessName);
        await page.keyboard.press(text);
        result = { success: true, action: "press", key: text };
        break;
      }
      case "upload": {
        if (!selector) return res.status(400).json({ success: false, error: "upload需要selector参数" });
        if (!file_path) return res.status(400).json({ success: false, error: "upload需要file_path参数" });
        const page = await getPage(sessName);
        const sel = await resolveSelector(page, selector);
        if (!sel) return res.status(400).json({ success: false, error: "无效的选择器" });
        try {
          await sel.locator.setInputFiles(file_path);
        } catch (uploadErr) {
          return res.status(500).json({ success: false, error: `upload失败: ${uploadErr?.message || String(uploadErr)}` });
        }
        result = { success: true, action: "upload", target: sel.desc, file: file_path };
        break;
      }
      case "drag": {
        if (!drag_from || !drag_to) {
          return res.status(400).json({ success: false, error: "drag需要drag_from和drag_to参数" });
        }
        const page = await getPage(sessName);
        const from = page.locator(drag_from);
        const to = page.locator(drag_to);
        await from.dragTo(to, { timeout: timeoutMs });
        result = { success: true, action: "drag", from: drag_from, to: drag_to };
        break;
      }
      case "extract": {
        const page = await getPage(sessName);
        const es = extract_selector || selector || "body";
        const content = await page.locator(es).first().textContent({ timeout: timeoutMs }).catch(() => null);
        const html = await page.locator(es).first().innerHTML({ timeout: timeoutMs }).catch(() => null);
        result = { success: true, action: "extract", selector: es, text: content?.trim() || null, html: html?.trim() || null };
        break;
      }
      case "pdf": {
        const page = await getPage(sessName);
        const buf = await page.pdf({ format: "A4", printBackground: true });
        const fname = `page_${Date.now()}.pdf`;
        const fpath = resolve(config.filesDir, fname);
        writeFileSync(fpath, buf);
        result = { success: true, action: "pdf", file: fname, url: `${config.baseUrl}/files/${fname}`, size: formatSize(buf.length) };
        break;
      }
      case "cookies": {
        const sess = browserSessions.get(sessName);
        if (!sess) return res.status(404).json({ success: false, error: "会话不存在" });
        if (text) {
          const cookies = JSON.parse(text);
          await sess.context.addCookies(cookies);
          result = { success: true, action: "cookies", operation: "set" };
        } else {
          const cookies = await sess.context.cookies();
          result = { success: true, action: "cookies", operation: "get", cookies };
        }
        break;
      }
      default:
        return res.status(400).json({
          success: false,
          error: `不支持的操作: ${action}`,
          supported_actions: ["open", "snapshot", "click", "fill", "type", "scroll", "hover", "screenshot", "back", "forward", "reload", "close", "execute", "wait", "resize", "select", "press", "upload", "drag", "extract", "pdf", "cookies"],
        });
    }

    const ms = Date.now() - t0;
    result.session = sessName;
    result.duration_ms = ms;
    result.duration_human = formatDuration(ms);
    return res.json(result);
  } catch (e) {
    logger.error(`浏览器API异常`, { action: req.body?.action, error: e.message });
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

app.get("/api/browser/sessions", (req, res) => {
  const now = Date.now();
  const sessions = [];
  for (const [name, sess] of browserSessions) {
    sessions.push({
      session_name: name,
      created_at: new Date(sess.createdAt).toISOString(),
      last_active_at: new Date(sess.lastActiveAt).toISOString(),
      idle_seconds: Math.floor((now - sess.lastActiveAt) / 1000),
      url: sess.page.url() || "about:blank",
      timeout_minutes: config.browserSessionTimeoutMs / 60000,
      browser_connected: sess.browser?.isConnected() || false,
    });
  }
  return res.json({
    success: true,
    total_sessions: sessions.length,
    max_sessions: config.browserMaxSessions,
    sessions,
  });
});

app.delete("/api/browser/:sessionName", async (req, res) => {
  try {
    const name = req.params.sessionName;
    if (browserSessions.has(name)) {
      const sess = browserSessions.get(name);
      try { await sess.context.close(); } catch(e) {}
      try { await sess.browser?.close(); } catch(e) {}
      browserSessions.delete(name);
    }
    return res.json({ success: true, message: `浏览器会话 ${name} 已关闭` });
  } catch (e) {
    return res.status(500).json({ success: false, error: e?.message || String(e) });
  }
});

// ============ MCP处理 ============
const handleMCP = async (req, res) => {
  const server = createServer();
  const transport = new StreamableHTTPServerTransport();
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch(e) {
    logger.error(`MCP请求出错`, { error: e.message });
    if (!res.headersSent) res.status(500).json({ error: e?.message || "MCP请求失败" });
  } finally {
    try { await transport.close(); } catch(e) {}
    try { await server.close(); } catch(e) {}
  }
};

app.post("/mcp", handleMCP);
app.get("/mcp", handleMCP);

// ============ 根路径 ============
app.get("/", (req, res) => {
  res.json({
    status: "ok",
    service: "MCP Terminal Service",
    version: "8.0.0",
    mcp_endpoint: "/mcp",
    config: {
      filesDir: config.filesDir,
      baseUrl: config.baseUrl,
      terminalTimeout: config.terminalTimeoutMs,
      browserTimeout: config.browserTimeoutMs,
    },
    endpoints: {
      health: "/health",
      search: "/api/search",
      terminal: "/api/terminal",
      terminal_sessions: "/api/terminal/sessions",
      files: "/files/:filename",
      file_download: "/api/file/download",
      file_upload: "/api/file/upload",
      file_delete: "/api/file/delete",
      browser: "/api/browser",
      browser_sessions: "/api/browser/sessions",
      background: "/api/background",
      mcp: "/mcp",
      ws_terminal: "/ws/terminal",
    },
    stats: {
      terminal_sessions: terminalSessions.size,
      pty_sessions: ptySessions.size,
      browser_sessions: browserSessions.size,
      background_tasks: bgTasks.size,
      uptime: process.uptime(),
      memory: Math.round(process.memoryUsage().rss / 1024 / 1024),
    },
  });
});

// ============ 404处理 ============
app.use((req, res) => {
  res.status(404).json({
    error: "路由不存在",
    available: [
      "/", "/health", "/mcp",
      "/api/search", "/api/terminal", "/api/terminal/sessions",
      "/api/file/download", "/api/file/upload", "/api/file/delete",
      "/api/browser", "/api/browser/sessions", "/api/background",
      "/files/:filename", "/ws/terminal",
    ],
  });
});

// ============ 全局异常处理 ============
process.on("uncaughtException", (e) => {
  logger.error(`未捕获异常`, { error: e.message, stack: e.stack });
});

process.on("unhandledRejection", (reason) => {
  logger.error(`未处理Promise拒绝`, { reason });
});

// ============ 启动服务器 ============
const httpServer = app.listen(config.port, config.host, () => {
  logger.info(`MCP服务已启动`, {
    host: config.host,
    port: config.port,
    version: "8.0.0",
    filesDir: config.filesDir,
    baseUrl: config.baseUrl,
    ptyAvailable,
  });
});

// ============ WebSocket终端服务 ============
if (ptyAvailable) {
  const wss = new WebSocketServer({ server: httpServer, path: "/ws/terminal" });

  wss.on("connection", (ws, req) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    let sessionId = url.searchParams.get("sessionId") || generateId('pty_');
    let cols = parseInt(url.searchParams.get("cols")) || 80;
    let rows = parseInt(url.searchParams.get("rows")) || 24;

    logger.info(`WebSocket连接`, { sessionId, cols, rows });

    const ptySession = getOrCreatePtySession(sessionId, cols, rows);

    const onPtyData = (data) => {
      if (ws.readyState === ws.OPEN) {
        ws.send(JSON.stringify({ type: "data", data: data.toString() }));
      }
    };
    ptySession.onData(onPtyData);

    let isAlive = true;
    const heartbeatTimer = setInterval(() => {
      if (ws.readyState !== ws.OPEN) {
        clearInterval(heartbeatTimer);
        return;
      }
      if (isAlive === false) {
        logger.info(`WebSocket心跳超时`, { sessionId });
        ws.terminate();
        clearInterval(heartbeatTimer);
        return;
      }
      isAlive = false;
      ws.send(JSON.stringify({ type: "ping" }));
    }, config.ptyHeartbeatInterval);

    ws.on("message", (message) => {
      try {
        const msg = JSON.parse(message.toString());
        switch (msg.type) {
          case "data":
            if (msg.data) ptySession.write(msg.data);
            break;
          case "resize":
            if (msg.cols && msg.rows) ptySession.resize(msg.cols, msg.rows);
            break;
          case "pong":
            isAlive = true;
            break;
          default:
            break;
        }
      } catch (e) {
        // 忽略非JSON消息
      }
    });

    ptySession._onExit = (code) => {
      logger.info(`PTY进程退出，关闭WebSocket`, { sessionId, code });
      try {
        if (ws.readyState === ws.OPEN) {
          ws.send(JSON.stringify({ type: "exit", code, message: "终端进程已退出" }));
        }
      } catch(e) {}
      setTimeout(() => {
        try { ws.terminate(); } catch(e) {}
      }, 100);
    };

    ws.on("close", () => {
      logger.info(`WebSocket连接关闭`, { sessionId });
      clearInterval(heartbeatTimer);
      ptySession.offData(onPtyData);
      ptySession._onExit = null;
      ptySession.destroy();
    });

    ws.on("error", (err) => {
      logger.error(`WebSocket错误`, { sessionId, error: err.message });
    });
  });

  // 为PtySession添加offData方法
  if (!PtySession.prototype.offData) {
    PtySession.prototype.offData = function(callback) {
      const idx = this._dataCallbacks.indexOf(callback);
      if (idx !== -1) this._dataCallbacks.splice(idx, 1);
      try {
        this.ptyProcess?.removeListener("data", callback);
      } catch (e) {}
    };
  }

  logger.info(`WebSocket终端服务已初始化`, { path: "/ws/terminal" });
} else {
  logger.warn(`WebSocket终端功能禁用: node-pty未安装`);
}

// ============ 启动完成日志 ============
logger.info(`服务启动完成`, {
  endpoints: {
    mcp: `http://${config.host}:${config.port}/mcp`,
    search: `http://${config.host}:${config.port}/api/search`,
    terminal: `http://${config.host}:${config.port}/api/terminal`,
    files: `http://${config.host}:${config.port}/files/`,
    browser: `http://${config.host}:${config.port}/api/browser`,
    ws: `ws://${config.host}:${config.port}/ws/terminal`,
  },
  pty_available: ptyAvailable,
});