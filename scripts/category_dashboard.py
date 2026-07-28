#!/usr/bin/env python3
"""classify_category.py가 실시간으로 append하고 있는 결과 파일(JSONL)을 읽어서, 카테고리별로
쌓이는 모습을 브라우저에서 보여주는 로컬 대시보드. classify_category.py랑 동시에 켜놓고 보면 된다.
서버가 매 요청마다 결과 파일을 다시 읽기만 할 뿐 아무것도 쓰지 않는다 — 분류 스크립트와는
완전히 독립적이라 대시보드를 껐다 켜도 진행 중인 분류에는 영향이 없다.

사용법:
    python3 scripts/category_dashboard.py                              # 기본 경로 + 기본 포트(8811)
    PORT=9000 python3 scripts/category_dashboard.py 결과.jsonl 입력.jsonl
그 다음 브라우저에서 http://localhost:8811 접속.
"""
import json
import os
import pathlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from common import CATEGORY_TAXONOMY

RESULT_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_result.jsonl'
INPUT_DEFAULT = pathlib.Path.home() / 'Desktop' / 'gonggu_category_input.jsonl'

RESULT_PATH = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else RESULT_DEFAULT
INPUT_PATH = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else INPUT_DEFAULT
PORT = int(os.environ.get('PORT', '8811'))

# common.CATEGORY_TAXONOMY(=classify_category.py가 쓰는 것과 동일)에서 카테고리 순서를 그대로 가져온다.
CATEGORIES = list(CATEGORY_TAXONOMY.keys())


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def build_stats():
    rows = _read_jsonl(RESULT_PATH)
    total = sum(1 for _ in _read_jsonl(INPUT_PATH)) if INPUT_PATH.exists() else 0

    by_cat = {c: [] for c in CATEGORIES}
    unmatched = []
    error_count = 0
    for r in rows:
        if r.get('classify_error'):
            error_count += 1
            continue
        cat = r.get('category')
        entry = {
            'name': r.get('product_name') or '',
            'sub': r.get('subcategory') or '',
            'confidence': r.get('confidence'),
            'reason': r.get('reason') or '',
            'llm_category': r.get('llm_category'),
            'llm_subcategory': r.get('llm_subcategory'),
        }
        if cat in by_cat:
            by_cat[cat].append(entry)
        else:
            unmatched.append({**entry, 'category': cat or ''})

    return {
        'total': total,
        'processed': len(rows),
        'error_count': error_count,
        'categories': [{'name': c, 'items': by_cat[c]} for c in CATEGORIES],
        'unmatched': unmatched,
        'taxonomy': CATEGORY_TAXONOMY,
    }


PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>공구 제품 카테고리 분류 — 실시간</title>
<style>
  :root {
    color-scheme: light;
    --surface-1:      #fcfcfb;
    --page:           #f9f9f7;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --muted:          #898781;
    --gridline:       #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --series-1:       #2a78d6;
    --series-1-track: color-mix(in oklab, #2a78d6 14%, transparent);
    --row-selected:   color-mix(in oklab, #2a78d6 8%, transparent);
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface-1:      #1a1a19;
      --page:           #0d0d0d;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --muted:          #898781;
      --gridline:       #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --series-1:       #3987e5;
      --series-1-track: color-mix(in oklab, #3987e5 18%, transparent);
      --row-selected:   color-mix(in oklab, #3987e5 14%, transparent);
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --surface-1:      #1a1a19;
    --page:           #0d0d0d;
    --text-primary:   #ffffff;
    --text-secondary: #c3c2b7;
    --muted:          #898781;
    --gridline:       #2c2c2a;
    --border:         rgba(255,255,255,0.10);
    --series-1:       #3987e5;
    --series-1-track: color-mix(in oklab, #3987e5 18%, transparent);
    --row-selected:   color-mix(in oklab, #3987e5 14%, transparent);
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 15px; font-weight: 600; margin: 0; }
  #themeToggle {
    border: 1px solid var(--border);
    background: var(--surface-1);
    color: var(--text-secondary);
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 13px;
  }

  .wrap { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding: 20px 24px; align-items: start; }
  @media (max-width: 900px) { .wrap { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }

  /* 진행률 스탯타일 + 미터 */
  .stat-row { display: flex; gap: 28px; margin-bottom: 14px; }
  .stat-label { color: var(--text-secondary); font-size: 12px; margin-bottom: 4px; }
  .stat-value { font-size: 26px; font-weight: 600; font-variant-numeric: proportional-nums; }
  .stat-value small { font-size: 14px; color: var(--text-secondary); font-weight: 500; }
  .meter-track {
    height: 8px; border-radius: 4px; background: var(--series-1-track); overflow: hidden;
  }
  .meter-fill {
    height: 100%; background: var(--series-1); border-radius: 4px;
    transition: width 0.4s ease;
  }

  /* 카테고리 랭크드 바 */
  .cat-list { list-style: none; margin: 14px 0 0; padding: 0; }
  .cat-row {
    display: grid;
    grid-template-columns: 108px 1fr 52px;
    align-items: center;
    gap: 10px;
    padding: 7px 8px;
    border-radius: 6px;
    cursor: pointer;
    background: transparent;
    border: 1px solid transparent;
  }
  .cat-row:hover { background: var(--row-selected); }
  .cat-row.selected { background: var(--row-selected); border-color: var(--series-1); }
  .cat-name { font-size: 13px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cat-track { height: 14px; border-radius: 4px; background: var(--series-1-track); overflow: hidden; }
  .cat-fill { height: 100%; background: var(--series-1); border-radius: 4px; transition: width 0.4s ease; }
  .cat-count { text-align: right; font-variant-numeric: tabular-nums; color: var(--text-secondary); font-size: 13px; }

  .card-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 10px; }
  .card-head h2 { font-size: 13px; font-weight: 600; margin: 0; color: var(--text-primary); }
  .card-head .count { font-size: 12px; color: var(--muted); }

  #viewToggle { border: none; background: none; color: var(--muted); font-size: 12px; cursor: pointer; text-decoration: underline; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  table th, table td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--gridline); }
  table td:last-child, table th:last-child { text-align: right; font-variant-numeric: tabular-nums; }

  /* 선택된 카테고리의 하위카테고리 건수 분해 */
  .subcat-list { list-style: none; margin: 0 0 4px; padding: 0; }
  .subcat-row {
    display: grid;
    grid-template-columns: 120px 1fr 40px;
    align-items: center;
    gap: 8px;
    padding: 4px 2px;
  }
  .subcat-name { font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .subcat-track { height: 8px; border-radius: 3px; background: var(--series-1-track); overflow: hidden; }
  .subcat-fill { height: 100%; background: var(--series-1); border-radius: 3px; transition: width 0.4s ease; opacity: 0.75; }
  .subcat-count { text-align: right; font-variant-numeric: tabular-nums; color: var(--muted); font-size: 12px; }
  .subcat-empty { color: var(--muted); font-size: 12px; padding: 4px 2px 10px; }

  #productPanel { max-height: 420px; overflow-y: auto; border-top: 1px solid var(--gridline); margin-top: 6px; }
  #productPanel .empty { color: var(--muted); padding: 24px 4px; text-align: center; }
  .product-item { padding: 8px 4px; border-bottom: 1px solid var(--gridline); }
  .product-item .pname { color: var(--text-primary); }
  .product-item .psub { color: var(--muted); font-size: 12px; margin-left: 6px; }
  .product-item .pconf { color: var(--muted); font-size: 11px; margin-left: 8px; font-variant-numeric: tabular-nums; }
  .product-item .preason { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }

  footer { padding: 8px 24px 20px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>공구 제품 카테고리 분류 — 실시간</h1>
  <button id="themeToggle">다크모드</button>
</header>

<div class="wrap">
  <div class="card">
    <div class="stat-row">
      <div>
        <div class="stat-label">처리 완료</div>
        <div class="stat-value"><span id="statProcessed">0</span> <small>/ <span id="statTotal">0</span></small></div>
      </div>
      <div>
        <div class="stat-label">진행률</div>
        <div class="stat-value" id="statPercent">0%</div>
      </div>
      <div>
        <div class="stat-label">실패</div>
        <div class="stat-value" id="statError">0</div>
      </div>
    </div>
    <div class="meter-track"><div class="meter-fill" id="meterFill" style="width:0%"></div></div>

    <div class="card-head" style="margin-top:20px;">
      <h2>카테고리별 건수</h2>
      <button id="viewToggle">표로 보기</button>
    </div>
    <ul class="cat-list" id="catList"></ul>
    <table id="catTable" style="display:none;">
      <thead><tr><th>카테고리</th><th>건수</th></tr></thead>
      <tbody id="catTableBody"></tbody>
    </table>
  </div>

  <div class="card">
    <div class="card-head">
      <h2 id="panelTitle">카테고리를 선택하세요</h2>
      <span class="count" id="panelCount"></span>
    </div>
    <ul class="subcat-list" id="subcatList"></ul>
    <div id="productPanel"><div class="empty">왼쪽에서 카테고리를 클릭하면 제품 목록이 여기 쌓입니다</div></div>
  </div>
</div>

<footer>1.5초마다 자동 갱신 · <span id="lastUpdated"></span></footer>

<script>
let selected = null;
let lastCounts = {};
let tableMode = false;

const themeToggle = document.getElementById('themeToggle');
function applyTheme(t) {
  if (t) { document.documentElement.setAttribute('data-theme', t); }
  else { document.documentElement.removeAttribute('data-theme'); }
  themeToggle.textContent = (document.documentElement.getAttribute('data-theme') === 'dark') ? '라이트모드' : '다크모드';
}
applyTheme(localStorage.getItem('cat-dash-theme') || '');
themeToggle.addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  localStorage.setItem('cat-dash-theme', next);
  applyTheme(next);
});

document.getElementById('viewToggle').addEventListener('click', () => {
  tableMode = !tableMode;
  document.getElementById('catList').style.display = tableMode ? 'none' : '';
  document.getElementById('catTable').style.display = tableMode ? '' : 'none';
  document.getElementById('viewToggle').textContent = tableMode ? '막대로 보기' : '표로 보기';
});

function fmt(n) { return n.toLocaleString('ko-KR'); }
function fmtConf(c) { return (typeof c === 'number' && !isNaN(c)) ? Math.round(c * 100) + '%' : ''; }

function renderProductPanel(cat, items) {
  document.getElementById('panelTitle').textContent = cat;
  document.getElementById('panelCount').textContent = fmt(items.length) + '건';
  const panel = document.getElementById('productPanel');
  const wasAtBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 12;
  const prevLen = lastCounts[cat] || 0;
  const builder = (cat === '미분류') ? makeUnmatchedItem : makeItem;

  if (items.length === 0) {
    panel.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = '아직 분류된 제품이 없습니다';
    panel.appendChild(empty);
  } else if (prevLen === 0 || panel.dataset.cat !== cat) {
    panel.innerHTML = '';
    for (const it of items) panel.appendChild(builder(it));
  } else if (items.length > prevLen) {
    for (const it of items.slice(prevLen)) panel.appendChild(builder(it));
  }
  panel.dataset.cat = cat;
  lastCounts[cat] = items.length;
  if (wasAtBottom) panel.scrollTop = panel.scrollHeight;
}

function renderSubcatBreakdown(cat, items, taxonomy) {
  const tally = {};
  for (const it of items) {
    const key = it.sub || '(없음)';
    tally[key] = (tally[key] || 0) + 1;
  }
  // taxonomy에 있는 하위카테고리는 0건이어도 목록에 보여준다 — 어떤 하위카테고리가
  // 실제로 하나도 안 나왔는지도 통계상 의미 있는 정보라 숨기지 않는다.
  const subs = (taxonomy && taxonomy[cat]) ? taxonomy[cat].slice() : Object.keys(tally);
  const rows = subs.map(s => ({ name: s, count: tally[s] || 0 }));
  rows.sort((a, b) => b.count - a.count);
  const maxN = Math.max(1, ...rows.map(r => r.count));

  const list = document.getElementById('subcatList');
  list.innerHTML = '';
  if (rows.length === 0) return;
  for (const r of rows) {
    const li = document.createElement('li');
    li.className = 'subcat-row';
    const name = document.createElement('div');
    name.className = 'subcat-name';
    name.textContent = r.name;
    const track = document.createElement('div');
    track.className = 'subcat-track';
    const fill = document.createElement('div');
    fill.className = 'subcat-fill';
    fill.style.width = Math.round((r.count / maxN) * 100) + '%';
    track.appendChild(fill);
    const count = document.createElement('div');
    count.className = 'subcat-count';
    count.textContent = fmt(r.count);
    li.appendChild(name); li.appendChild(track); li.appendChild(count);
    list.appendChild(li);
  }
}

function selectCategory(cat, items, taxonomy) {
  selected = cat;
  renderSubcatBreakdown(cat, items, taxonomy);
  renderProductPanel(cat, items);
}

function makeItem(it) {
  const div = document.createElement('div');
  div.className = 'product-item';
  const name = document.createElement('span');
  name.className = 'pname';
  name.textContent = it.name;
  div.appendChild(name);
  const sub = document.createElement('span');
  sub.className = 'psub';
  sub.textContent = it.sub;
  div.appendChild(sub);
  const c = fmtConf(it.confidence);
  if (c) {
    const conf = document.createElement('span');
    conf.className = 'pconf';
    conf.textContent = c;
    div.appendChild(conf);
  }
  return div;
}

function makeUnmatchedItem(it) {
  const div = document.createElement('div');
  div.className = 'product-item';
  const name = document.createElement('span');
  name.className = 'pname';
  name.textContent = it.name;
  div.appendChild(name);
  const c = fmtConf(it.confidence);
  if (c) {
    const conf = document.createElement('span');
    conf.className = 'pconf';
    conf.textContent = '신뢰도 ' + c;
    div.appendChild(conf);
  }
  const guess = [it.llm_category, it.llm_subcategory].filter(Boolean).join(' > ');
  const guessLine = document.createElement('span');
  guessLine.className = 'preason';
  guessLine.textContent = guess ? ('LLM 원래 판단: ' + guess) : (it.category ? ('원본 category 값: ' + it.category) : '');
  div.appendChild(guessLine);
  if (it.reason) {
    const reasonLine = document.createElement('span');
    reasonLine.className = 'preason';
    reasonLine.textContent = '이유: ' + it.reason;
    div.appendChild(reasonLine);
  }
  return div;
}

async function poll() {
  let data;
  try {
    const res = await fetch('/api/stats', {cache: 'no-store'});
    data = await res.json();
  } catch (e) {
    return;
  }

  document.getElementById('statProcessed').textContent = fmt(data.processed);
  document.getElementById('statTotal').textContent = fmt(data.total);
  document.getElementById('statError').textContent = fmt(data.error_count);
  const pct = data.total ? Math.round((data.processed / data.total) * 100) : 0;
  document.getElementById('statPercent').textContent = pct + '%';
  document.getElementById('meterFill').style.width = pct + '%';

  const maxCount = Math.max(1, ...data.categories.map(c => c.items.length));
  const catList = document.getElementById('catList');
  const tbody = document.getElementById('catTableBody');
  catList.innerHTML = '';
  tbody.innerHTML = '';

  for (const c of data.categories) {
    const n = c.items.length;
    const li = document.createElement('li');
    li.className = 'cat-row' + (selected === c.name ? ' selected' : '');
    const name = document.createElement('div');
    name.className = 'cat-name';
    name.textContent = c.name;
    const track = document.createElement('div');
    track.className = 'cat-track';
    const fill = document.createElement('div');
    fill.className = 'cat-fill';
    fill.style.width = Math.round((n / maxCount) * 100) + '%';
    track.appendChild(fill);
    const count = document.createElement('div');
    count.className = 'cat-count';
    count.textContent = fmt(n);
    li.appendChild(name); li.appendChild(track); li.appendChild(count);
    li.addEventListener('click', () => { selectCategory(c.name, c.items, data.taxonomy); poll(); });
    catList.appendChild(li);

    const tr = document.createElement('tr');
    const td1 = document.createElement('td'); td1.textContent = c.name;
    const td2 = document.createElement('td'); td2.textContent = fmt(n);
    tr.appendChild(td1); tr.appendChild(td2);
    tbody.appendChild(tr);
  }

  if (data.unmatched && data.unmatched.length) {
    const li = document.createElement('li');
    li.className = 'cat-row' + (selected === '미분류' ? ' selected' : '');
    li.style.opacity = '0.7';
    const name = document.createElement('div'); name.className = 'cat-name'; name.textContent = '미분류';
    const track = document.createElement('div'); track.className = 'cat-track';
    const fill = document.createElement('div'); fill.className = 'cat-fill';
    fill.style.width = Math.round((data.unmatched.length / maxCount) * 100) + '%';
    fill.style.background = 'var(--muted)';
    track.appendChild(fill);
    const count = document.createElement('div'); count.className = 'cat-count'; count.textContent = fmt(data.unmatched.length);
    li.appendChild(name); li.appendChild(track); li.appendChild(count);
    li.addEventListener('click', () => { selectCategory('미분류', data.unmatched, null); poll(); });
    catList.appendChild(li);
  }

  if (selected) {
    const cat = data.categories.find(c => c.name === selected);
    if (cat) { renderSubcatBreakdown(selected, cat.items, data.taxonomy); renderProductPanel(selected, cat.items); }
    else if (selected === '미분류') { renderSubcatBreakdown('미분류', data.unmatched || [], null); renderProductPanel('미분류', data.unmatched || []); }
  }

  document.getElementById('lastUpdated').textContent = '마지막 갱신 ' + new Date().toLocaleTimeString('ko-KR');
}

poll();
setInterval(poll, 1500);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 콘솔 스팸 방지 — 필요하면 지우고 기본 로깅 쓰면 됨

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            body = PAGE.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/api/stats':
            body = json.dumps(build_stats(), ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    print(f'대시보드: http://localhost:{PORT}')
    print(f'  결과 파일: {RESULT_PATH}')
    print(f'  입력 파일(전체 건수 기준): {INPUT_PATH}')
    ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever()


if __name__ == '__main__':
    main()
