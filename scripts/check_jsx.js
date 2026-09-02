#!/usr/bin/env node
/*
 * Parse every <script type="text/babel"> block in the dashboard as real JSX.
 *
 * WHY THIS EXISTS. index.html carries ~9,000 lines of in-browser-transpiled
 * JSX. A syntax error there does not degrade gracefully -- Babel fails, React
 * never mounts, and the ENTIRE dashboard is a blank page. On 2026-09-02 six
 * releases shipped in one afternoon validated only by counting brackets,
 * which is not a syntax check: a stray brace inside a string, an unclosed JSX
 * attribute, or a hook declared after an early return all pass it.
 *
 * There is no local Babel in this repo by default, so this is opt-in:
 *
 *     npm install --no-save @babel/parser
 *     node scripts/check_jsx.js stan/dashboard/public/index.html
 *
 * Exit 0 = every block parses. Exit 1 = a block failed, with the line number
 * in index.html (not in the extracted block, which is what you actually need).
 */
const fs = require('fs');
const path = require('path');

let parse;
try {
  ({ parse } = require('@babel/parser'));
} catch {
  console.error('@babel/parser not found. Run:  npm install --no-save @babel/parser');
  process.exit(2);
}

const file = process.argv[2] || 'stan/dashboard/public/index.html';
if (!fs.existsSync(file)) {
  console.error(`no such file: ${file}`);
  process.exit(2);
}
const html = fs.readFileSync(file, 'utf8');
const re = /<script[^>]*type=["']text\/babel["'][^>]*>([\s\S]*?)<\/script>/g;

let m, n = 0, bad = 0;
while ((m = re.exec(html))) {
  n++;
  const code = m[1];
  // Offset so the reported line is the line in index.html, not in the block.
  const line0 = html.slice(0, m.index).split('\n').length;
  try {
    parse(code, { sourceType: 'script', plugins: ['jsx'], errorRecovery: false });
    console.log(`block ${n} (from line ~${line0}): ${code.split('\n').length} lines  OK`);
  } catch (e) {
    bad++;
    const at = e.loc ? ` at ${path.basename(file)} line ~${line0 + e.loc.line - 1}, col ${e.loc.column}` : '';
    console.log(`block ${n}: SYNTAX ERROR${at}\n   ${e.message}`);
  }
}
if (!n) { console.log('no <script type="text/babel"> blocks found'); process.exit(2); }
console.log(bad ? `\n${bad} of ${n} block(s) FAILED` : `\nall ${n} babel block(s) parse cleanly`);
process.exit(bad ? 1 : 0);
