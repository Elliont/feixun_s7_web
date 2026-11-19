// 韭菜称重 · 前端交互
// 只做四件事：二次确认（data-confirm，自定义对话框）、自动提交（data-autosubmit）、
// 可点击表格行（data-href）、步进器。业务与渲染全在服务端。
(() => {
  'use strict';

  // Service Worker（离线壳）——曾内联在 base.html，CSP 禁内联脚本后外移
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }

  /* ---------- 自定义确认对话框（替代浏览器原生 confirm） ---------- */
  let dlgEl = null;

  const buildDialog = () => {
    const d = document.createElement('dialog');
    d.className = 'confirm-dialog';
    d.innerHTML =
      '<div class="confirm-body">' +
        '<p class="confirm-title">请确认</p>' +
        '<p class="confirm-text"></p>' +
      '</div>' +
      '<div class="confirm-actions">' +
        '<button type="button" class="btn btn-ghost" data-act="cancel">取消</button>' +
        '<button type="button" class="btn btn-primary" data-act="ok">确认</button>' +
      '</div>';
    document.body.appendChild(d);
    d.addEventListener('click', (ev) => {
      const act = ev.target && ev.target.dataset ? ev.target.dataset.act : null;
      if (act) {
        if (d._resolve) d._resolve(act === 'ok');
        d.close();
      } else if (ev.target === d) {           // 点遮罩
        if (d._resolve) d._resolve(false);
        d.close();
      }
    });
    d.addEventListener('cancel', (ev) => {    // Esc
      ev.preventDefault();
      if (d._resolve) d._resolve(false);
      d.close();
    });
    return d;
  };

  const askConfirm = (msg, danger) => new Promise((resolve) => {
    if (!dlgEl) dlgEl = buildDialog();
    dlgEl.querySelector('.confirm-text').textContent = msg;
    const okBtn = dlgEl.querySelector('[data-act="ok"]');
    okBtn.className = danger ? 'btn btn-danger' : 'btn btn-primary';
    okBtn.textContent = danger ? '确认删除' : '确认';
    dlgEl._resolve = resolve;
    dlgEl.showModal();
    okBtn.focus();
  });


  // 选择文件后立即提交（导入 CSV）——CSP 禁内联 onchange
  document.querySelectorAll('input[type=file][data-upload-submit]').forEach((inp) => {
    inp.addEventListener('change', () => { if (inp.files.length) inp.form.submit(); });
  });
  // 提交前拦截：data-confirm 可放在提交按钮上（兼容 formaction）或表单上
  document.addEventListener('submit', (e) => {
    const btn = e.submitter;
    const msg = (btn && btn.dataset.confirm) || e.target.dataset.confirm;
    if (!msg) return;
    e.preventDefault();
    const danger = !!(btn && (btn.classList.contains('btn-danger') ||
      btn.classList.contains('chip-danger'))) || e.target.dataset.confirmDanger === '1';
    askConfirm(msg, danger).then((yes) => {
      if (!yes) return;
      const form = e.target;
      if (btn && btn.getAttribute('formaction')) {
        form.action = btn.getAttribute('formaction');
      }
      form.submit();          // 直接提交，不再触发 submit 事件
    });
  });

  // 自动提交：用事件委托兼容 #rec-wrap 局部刷新后新插入的表单/控件
  document.addEventListener('change', (e) => {
    const el = e.target;
    const target = el.closest && el.closest('[data-autosubmit]');
    if (!target) return;
    const form = target.tagName === 'FORM' ? target : target.form;
    if (form) form.submit();
  });

  // 用户菜单：点击菜单外任意处收起（details 不会自动关闭）
  document.addEventListener('click', (e) => {
    document.querySelectorAll('details.user-menu[open]').forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute('open');
    });
  });

  // 表格整行可点（成员详情「体重记录」/ 称重记录）；行内链接、按钮、表单控件优先
  document.addEventListener('click', (e) => {
    const row = e.target.closest('tr[data-href]');
    if (!row || e.target.closest('a, button, select, input, label, form')) return;
    window.location.href = row.dataset.href;
  });

  /* ---------- 步进器（[−] 值 [+]） ---------- */
  document.querySelectorAll('.stepper').forEach((box) => {
    const input = box.querySelector('input');
    const stepStr = box.dataset.step || '1';
    const step = Number(stepStr);
    const decimals = (stepStr.split('.')[1] || '').length;
    const min = Number(box.dataset.min || 0);
    const max = Number(box.dataset.max || 9999);
    const optional = box.dataset.optional === '1';

    const apply = (delta) => {
      if (optional && input.value === '' && delta < 0) return;
      const cur = input.value === '' ? min : Number(input.value);
      let v = cur + delta;
      v = Math.max(min, Math.min(max, v));
      // 按步长小数位舍入，避免 60.2 + 0.1 = 60.300000000000004 触发 HTML5 step 校验失败
      input.value = decimals > 0 ? Number(v.toFixed(decimals)) : Math.round(v);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    };
    box.querySelector('[data-minus]').addEventListener('click', () => apply(-step));
    box.querySelector('[data-plus]').addEventListener('click', () => apply(step));
  });

  // 指标卡「?」：点击/回车弹出计算方法说明（全局共享一个浮层，点别处/滚动关闭）
  let tipEl = null;
  const hideTip = () => { if (tipEl) { tipEl.remove(); tipEl = null; } };
  const tipLabelOf = (q) => q.dataset.label
    || (q.closest('.is-metric') && q.closest('.is-metric').dataset.label)
    || (q.closest('.core-card, .metric')?.querySelector('.core-label, .metric-label')?.textContent)
    || '说明';

  function toggleTip(q) {
    if (tipEl && tipEl._for === q) { hideTip(); return; }
    hideTip();
    tipEl = document.createElement('div');
    tipEl.className = 'tip-pop';
    tipEl._for = q;
    const b = document.createElement('b');
    b.textContent = tipLabelOf(q);
    tipEl.appendChild(b);
    tipEl.appendChild(document.createTextNode(q.dataset.tip || ''));
    document.body.appendChild(tipEl);
    const r = q.getBoundingClientRect();
    const pw = tipEl.offsetWidth, ph = tipEl.offsetHeight;
    let x = r.right + window.scrollX + 8;
    if (x + pw > window.scrollX + document.documentElement.clientWidth - 8) {
      x = r.left + window.scrollX - pw - 8;
    }
    let y = r.top + window.scrollY - 6;
    y = Math.max(window.scrollY + 4,
                 Math.min(y, window.scrollY + window.innerHeight - ph - 4));
    tipEl.style.left = x + 'px';
    tipEl.style.top = y + 'px';
  }

  document.addEventListener('click', (e) => {
    const q = e.target.closest('.qmark');
    if (q) { e.preventDefault(); e.stopPropagation(); toggleTip(q); return; }
    if (tipEl && !e.target.closest('.tip-pop')) hideTip();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { hideTip(); return; }
    const q = e.target.closest && e.target.closest('.qmark');
    if (q && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault(); e.stopPropagation(); toggleTip(q);
    }
  });
  window.addEventListener('scroll', hideTip, true);


  /* ---------- 设备 tab 上的改名 ---------- */
  const renameForm = document.querySelector('form.dev-rename');
  if (renameForm) {
    const nameInput = renameForm.querySelector('input[name="name"]');
    const macInput = renameForm.querySelector('input[name="mac"]');
    document.querySelectorAll('.mtab-edit').forEach((btn) => {
      btn.addEventListener('click', () => {
        macInput.value = btn.dataset.mac;
        nameInput.value = btn.dataset.name || '';
        nameInput.placeholder = btn.dataset.display || '设备名';
        renameForm.hidden = false;
        nameInput.focus();
        nameInput.select();
      });
    });
    renameForm.querySelector('[data-rename-cancel]').addEventListener('click', () => {
      renameForm.hidden = true;
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !renameForm.hidden) renameForm.hidden = true;
    });
  }

  /* ---------- 设备绑定：下拉里勾选即生效（fetch 提交，不刷新） ---------- */
  const bindForm = document.querySelector('form.dev-pick-body');
  if (bindForm) {
    const allBox = bindForm.querySelector('[data-dev-all]');
    const memberBoxes = [...bindForm.querySelectorAll('input[name="member_id"]')];
    const sumEl = document.querySelector('[data-pick-sum]');
    const summary = () => {
      if (allBox.checked) return '全部成员';
      const names = memberBoxes.filter((b) => b.checked)
        .map((b) => b.closest('label').querySelector('span').textContent.trim());
      return names.length ? names.join('、') : '未绑定';
    };
    bindForm.addEventListener('change', async (e) => {
      if (e.target === allBox) {
        // 勾「全部成员」→ 单人框清空并置灰；取消 → 可逐个选，也可以一个都不选
        memberBoxes.forEach((b) => {
          b.checked = false;
          b.disabled = allBox.checked;
        });
      } else {
        allBox.checked = false;
        memberBoxes.forEach((b) => { b.disabled = false; });
      }
      if (sumEl) sumEl.textContent = summary();
      try {
        const res = await fetch(bindForm.action, {
          method: 'POST', body: new FormData(bindForm), credentials: 'same-origin',
        });
        if (!res.ok) location.reload();
      } catch (err) {
        location.reload();
      }
    });
  }

  /* ---------- 成员表单：出生日期 → 周岁提示 ---------- */

  /* ---------- 指标趋势图：点指标卡切换，SVG 折线 + 面积 + 十字线 ---------- */
  const trendBox = document.getElementById('trend');
  if (trendBox) {
    const stage = document.getElementById('trend-stage');
    const elTitle = document.getElementById('trend-title');
    const elNow = document.getElementById('trend-now');
    const elVerdict = document.getElementById('trend-verdict');
    const color = trendBox.dataset.color || '#00A98F';
    const cards = [...document.querySelectorAll('.is-metric')];
    /* 默认体重趋势；卡片选中=该指标趋势，再点一次取消选中回到体重趋势 */
    const allTriggers = cards;
    const SVGNS = 'http://www.w3.org/2000/svg';
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let DATA = null, days = 90, current = 'weight';

    const svgEl = (tag, attrs) => {
      const n = document.createElementNS(SVGNS, tag);
      for (const k in attrs) n.setAttribute(k, attrs[k]);
      return n;
    };
    const fmt = (v, unit) => unit === '' ? String(v) : v + ' ' + unit;

    // Catmull-Rom → cubic Bézier：曲线平滑过每一点，控制点夹在绘图区内防溢出
    function smoothD(xs, ys, top, bottom) {
      const n = xs.length, P = v => v.toFixed(1);
      if (n < 3) return xs.map((x, i) => `${i ? 'L' : 'M'}${P(x)},${P(ys[i])}`).join(' ');
      const clamp = v => Math.max(top, Math.min(bottom, v));
      let d = `M${P(xs[0])},${P(ys[0])}`;
      for (let i = 0; i < n - 1; i++) {
        const x0 = xs[i - 1] ?? xs[i], y0 = ys[i - 1] ?? ys[i];
        const x1 = xs[i], y1 = ys[i];
        const x2 = xs[i + 1], y2 = ys[i + 1];
        const x3 = xs[i + 2] ?? x2, y3 = ys[i + 2] ?? y2;
        const c1x = x1 + (x2 - x0) / 6, c1y = clamp(y1 + (y2 - y0) / 6);
        const c2x = x2 - (x3 - x1) / 6, c2y = clamp(y2 - (y3 - y1) / 6);
        d += ` C${P(c1x)},${P(c1y)} ${P(c2x)},${P(c2y)} ${P(x2)},${P(y2)}`;
      }
      return d;
    }

    function series(key) {
      const cut = days ? (() => {
        const d = new Date(); d.setDate(d.getDate() - days);
        return d.toISOString().slice(0, 10);
      })() : '';
      return DATA.filter(p => p[key] !== null && p[key] !== undefined && (!cut || p.d >= cut));
    }

    function draw() {
      const card = allTriggers.find(c => c.dataset.key === current);
      const unit = card ? card.dataset.unit : '';
      const label = card ? card.dataset.label : '体重';
      const verdict = card ? (card.dataset.verdict || '') : '';
      const pts = series(current);
      elTitle.textContent = label;
      elVerdict.textContent = verdict;
      elVerdict.className = 'chart-verdict' + (card ? ' lv-' + (card.className.match(/lv-(\w+)/) || [])[1] : '');
      elNow.textContent = pts.length ? fmt(pts[pts.length - 1][current], unit) : '';

      stage.innerHTML = '';
      if (pts.length < 2) {
        const p = document.createElement('p');
        p.className = 'chart-note';
        p.textContent = pts.length === 1 ? '只有 1 条数据，再称一次就能画出走势' : '这段时间还没有数据';
        stage.appendChild(p);
        return;
      }

      const box = stage.getBoundingClientRect();
      const W = Math.max(320, Math.round(box.width || 900)), H = 246;
      const padL = 46, padR = 16, padT = 14, padB = 26;
      const iw = W - padL - padR, ih = H - padT - padB;

      const vals = pts.map(p => Number(p[current]));
      let lo = Math.min(...vals), hi = Math.max(...vals);
      if (lo === hi) { lo -= 1; hi += 1; }
      const padV = (hi - lo) * 0.14;
      lo -= padV; hi += padV;

      // X 轴按真实时间戳比例映射，避免疏密不均的称重扭曲趋势斜率
      const ts = pts.map(p => Date.parse(String(p.t || '').replace(' ', 'T')));
      const validTs = ts.every(Number.isFinite);
      const tMin = validTs ? ts[0] : 0;
      const tSpan = validTs ? (ts[ts.length - 1] - tMin) : 0;
      const xs = pts.map((_, i) => {
        if (pts.length === 1) return padL;
        if (validTs && tSpan > 0) return padL + iw * (ts[i] - tMin) / tSpan;
        return padL + iw * i / (pts.length - 1);
      });
      const ys = vals.map(v => padT + ih * (1 - (v - lo) / (hi - lo)));
      const X = i => xs[i], Y = i => ys[i];

      const svg = svgEl('svg', {
        class: 'chart-svg', viewBox: `0 0 ${W} ${H}`, width: '100%', height: H,
        role: 'img', 'aria-label': `${label}走势，${pts.length} 个数据点`
      });

      // Y 轴网格 + 刻度
      const grid = svgEl('g', { class: 'chart-grid' });
      const axis = svgEl('g', { class: 'chart-axis' });
      const ticks = 4;
      const digits = (hi - lo) < 6 ? 1 : 0;
      for (let i = 0; i <= ticks; i++) {
        const v = lo + (hi - lo) * i / ticks;
        const y = padT + ih * (1 - i / ticks);
        grid.appendChild(svgEl('line', { x1: padL, x2: W - padR, y1: y, y2: y }));
        const t = svgEl('text', { x: padL - 8, y: y + 4, 'text-anchor': 'end' });
        t.textContent = v.toFixed(digits);
        axis.appendChild(t);
      }
      // X 轴日期刻度（最多 4 个，避免拥挤）
      const step = Math.max(1, Math.ceil(pts.length / 4));
      for (let i = 0; i < pts.length; i += step) {
        const t = svgEl('text', { x: X(i), y: H - 8, 'text-anchor': i === 0 ? 'start' : 'middle' });
        t.textContent = pts[i].d.slice(5).replace('-', '/');
        axis.appendChild(t);
      }
      svg.appendChild(grid); svg.appendChild(axis);
      // 面积 + 折线
      const lineD = smoothD(xs, ys, padT, padT + ih);
      const areaD = `${lineD} L${X(pts.length - 1).toFixed(1)},${padT + ih} L${padL},${padT + ih} Z`;
      const gid = 'g' + Math.random().toString(36).slice(2, 8);
      const defs = svgEl('defs', {});
      const grad = svgEl('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 });
      const s1 = svgEl('stop', { offset: '0%', 'stop-color': color, 'stop-opacity': .22 });
      const s2 = svgEl('stop', { offset: '100%', 'stop-color': color, 'stop-opacity': 0 });
      grad.appendChild(s1);
      grad.appendChild(s2);
      defs.appendChild(grad);
      svg.appendChild(defs);

      const area = svgEl('path', { d: areaD, fill: `url(#${gid})`, class: 'chart-area' });
      const line = svgEl('path', {
        d: lineD, fill: 'none', stroke: color, 'stroke-width': 2.4, class: 'chart-line'
      });
      svg.appendChild(area); svg.appendChild(line);

      // 数据点：点多时只画首尾与末点，避免糊成一片
      const dotIdx = pts.length <= 40 ? pts.map((_, i) => i)
        : [...new Set([0, pts.length - 1, pts.length - 2])].filter(i => i >= 0);
      dotIdx.forEach((i, k) => {
        const c = svgEl('circle', {
          cx: X(i), cy: Y(i), r: 3.2, fill: '#fff', stroke: color, 'stroke-width': 2,
          class: 'chart-dot'
        });
        svg.appendChild(c);
      });

      // 十字线 + 高亮点 + hover 热区
      const cross = svgEl('line', {
        class: 'chart-cross', x1: 0, x2: 0, y1: padT, y2: padT + ih
      });
      const focus = svgEl('circle', {
        r: 4.5, fill: color, stroke: '#fff', 'stroke-width': 2, opacity: 0
      });
      svg.appendChild(cross); svg.appendChild(focus);

      const hit = svgEl('rect', {
        class: 'chart-hit', x: padL - 6, y: padT, width: iw + 12, height: ih
      });
      svg.appendChild(hit);
      stage.appendChild(svg);

      const tip = document.createElement('div');
      tip.className = 'chart-tip';
      stage.appendChild(tip);

      const nearIdx = (ev) => {
        const r = svg.getBoundingClientRect();
        const sx = (ev.clientX - r.left) / r.width * W;
        let best = 0, bestD = Infinity;
        xs.forEach((x, i) => { const d = Math.abs(x - sx); if (d < bestD) { bestD = d; best = i; } });
        return best;
      };
      const moveTip = (ev) => {
        const r = svg.getBoundingClientRect();
        const best = nearIdx(ev);
        cross.setAttribute('x1', X(best));
        cross.setAttribute('x2', X(best));
        cross.style.opacity = 1;
        focus.setAttribute('cx', X(best)); focus.setAttribute('cy', Y(best));
        focus.setAttribute('opacity', 1);
        tip.innerHTML = `<b>${fmt(vals[best], unit)}</b><i>${pts[best].t}</i>`;
        tip.style.left = (X(best) / W * r.width) + 'px';
        tip.style.top = ((Y(best) / H) * r.height - 10) + 'px';
        tip.style.opacity = 1;
      };
      const leaveTip = () => {
        cross.style.opacity = 0; focus.setAttribute('opacity', 0); tip.style.opacity = 0;
      };
      hit.addEventListener('pointermove', moveTip);
      hit.addEventListener('pointerleave', leaveTip);
      // 点数据点 → 打开那一次称重记录
      hit.addEventListener('click', (ev) => {
        const p = pts[nearIdx(ev)];
        const mid = trendBox.dataset.member;
        if (p && p.rid && mid) location.href = '/m/' + mid + '/r/' + p.rid;
      });

      // 入场动画：折线自左向右绘出
      if (!reduce) {
        const len = line.getTotalLength();
        line.style.strokeDasharray = len;
        line.style.strokeDashoffset = len;
        line.style.transition = 'stroke-dashoffset .72s cubic-bezier(.16,1,.3,1)';
        requestAnimationFrame(() => { line.style.strokeDashoffset = 0; });
      }
    }

    function selectCard(key) {
      if (key === current) key = 'weight';      // 再点一次取消选中 → 回到体重趋势
      current = key;
      cards.forEach(c => {
        const on = c.dataset.key === key;
        c.classList.toggle('is-active', on);
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      draw();
    }

    cards.forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.target.closest('.qmark')) return;   // 问号只开说明，不切趋势
        selectCard(card.dataset.key);
        const top = document.getElementById('trend').getBoundingClientRect().top + window.scrollY - 70;
        if (window.scrollY > top + 40) window.scrollTo({ top, behavior: reduce ? 'auto' : 'smooth' });
      });
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          if (e.target.closest('.qmark')) return;
          e.preventDefault();
          selectCard(card.dataset.key);
        }
      });
    });



    trendBox.querySelectorAll('.seg').forEach(btn => {
      btn.addEventListener('click', () => {
        days = Number(btn.dataset.days);
        trendBox.querySelectorAll('.seg').forEach(b => b.classList.toggle('is-on', b === btn));
        draw();
      });
    });

    let rz = null;
    window.addEventListener('resize', () => {
      clearTimeout(rz);
      rz = setTimeout(draw, 160);
    });

    fetch(trendBox.dataset.src, { credentials: 'same-origin' })
      .then(r => r.json())
      .then(j => {
        if (!j.ok) throw new Error('bad payload');
        DATA = j.points || [];
        selectCard('weight');          // 默认体重趋势，不预选任何指标卡
      })
      .catch((err) => {
        console.error('[trend] 加载或绘制失败', err);
        const p = document.createElement('p');
        p.textContent = '趋势加载失败，刷新后重试';
        stage.innerHTML = '';
        stage.appendChild(p);
      });
  }

  /* ---------- 体重记录：换页 / 每页条数 / 排序 / 列切换 —— 局部刷新，不跳回页顶 ---------- */
  const recWrap = document.getElementById('rec-wrap');
  if (recWrap) {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    let busy = false;

    const fade = (v) => {
      recWrap.style.transition = reduce ? 'none' : 'opacity .16s ease';
      recWrap.style.opacity = v;
    };

    const load = (url, push) => {
      if (!url || busy) return;
      busy = true;
      const focusId = document.activeElement && recWrap.contains(document.activeElement)
        ? document.activeElement.id : '';
      fade('0.35');
      fetch(url, { credentials: 'same-origin' })
        .then(r => (r.ok ? r.text() : Promise.reject(new Error('HTTP ' + r.status))))
        .then((html) => {
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const next = doc.getElementById('rec-wrap');
          if (!next) throw new Error('missing #rec-wrap');
          recWrap.innerHTML = next.innerHTML;
          if (push) history.pushState({ rec: 1 }, '', url);
          if (focusId === 'rs') {
            const again = recWrap.querySelector('#rs');
            if (again) again.focus();
          }
          fade('1');
        })
        .catch(() => { location.href = url; })   // 兜底：整页跳转，功能不丢
        .then(() => { busy = false; });
    };

    document.addEventListener('change', (e) => {
      const sel = e.target;
      if (!sel.matches || !sel.matches('[data-rec-size]')) return;
      const u = new URL(location.href);
      const key = sel.name || 'rs';          // 详情页是 rs，称重记录页是 size
      u.searchParams.set(key, sel.value);
      u.searchParams.delete('rp');
      u.searchParams.delete('page');         // 换每页条数回到第一页
      load(u.pathname + u.search, true);
    });

    document.addEventListener('click', (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
      const a = e.target.closest && e.target.closest('#rec-wrap a.th-sort, #rec-wrap .pager a, #rec-wrap .col-chip');
      if (!a || (a.target && a.target !== '_self')) return;
      e.preventDefault();
      load(a.getAttribute('href'), true);
    });


    /* ---------- 列筛选面板（Excel 风格）：全选 / 搜索 / 确定后才生效 ---------- */
    const syncAllBox = (menu) => {
      const all = menu.querySelector('[data-col-all]');
      if (!all) return;
      const boxes = [...menu.querySelectorAll('.col-list input[type=checkbox]')]
        .filter(b => b !== all);
      all.checked = boxes.length > 0 && boxes.every(b => b.checked);
    };
    document.addEventListener('change', (e) => {
      const menu = e.target.closest && e.target.closest('#col-menu');
      if (!menu) return;
      if (e.target.matches('[data-col-all]')) {
        menu.querySelectorAll('.col-list input[type=checkbox]').forEach(b => {
          if (b !== e.target) b.checked = e.target.checked;
        });
      }
      syncAllBox(menu);
    });
    document.addEventListener('input', (e) => {
      if (!e.target.matches || !e.target.matches('#col-menu .col-search')) return;
      const menu = e.target.closest('#col-menu');
      const q = e.target.value.trim();
      menu.querySelectorAll('.col-opt[data-label]').forEach((el) => {
        el.hidden = !!q && !el.dataset.label.includes(q);
      });
    });
    document.addEventListener('click', (e) => {
      const cancel = e.target.closest && e.target.closest('#col-menu [data-col-cancel]');
      if (!cancel) return;
      cancel.closest('#col-menu').open = false;
    });
    document.addEventListener('submit', (e) => {
      const form = e.target.closest && e.target.closest('.col-filter');
      if (!form) return;
      e.preventDefault();
      const hint = form.querySelector('.col-hint');
      if (!form.querySelector('input[name=rc]:checked')) {
        if (hint) hint.hidden = false;
        return;
      }
      if (hint) hint.hidden = true;
      const u = new URL(form.getAttribute('action'), location.origin);
      new FormData(form).forEach((v, k) => u.searchParams.append(k, v));
      const menu = form.closest('#col-menu');
      if (menu) menu.open = false;
      load(u.pathname + u.search, true);
    });
    window.addEventListener('popstate', () => load(location.pathname + location.search, false));
  }
  const dateInput = document.getElementById('birth_date');
  const ageHint = document.getElementById('age-hint');
  if (dateInput && ageHint) {
    const update = () => {
      const v = dateInput.value;
      if (!v) { ageHint.textContent = '—'; return; }
      const [y, m, d] = v.split('-').map(Number);
      const now = new Date();
      let age = now.getFullYear() - y;
      if (now.getMonth() + 1 < m || (now.getMonth() + 1 === m && now.getDate() < d)) age -= 1;
      ageHint.textContent = String(Math.max(0, age));
    };
    dateInput.addEventListener('input', update);
    dateInput.addEventListener('change', update);
    update();
  }
})();
