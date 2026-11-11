/* WorkOS · мини-приложение «Квесты» (Telegram Mini App). Без библиотек, данные из /api.
 * Демо без Telegram: ?demo=1. Просмотр живых данных в браузере: #dev=<APP_DEV_TOKEN>. */
(function () {
  'use strict';

  // ---------- окружение ----------
  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  const qs = new URLSearchParams(location.search);
  const hs = new URLSearchParams(location.hash.replace(/^#/, ''));
  const INIT = tg && tg.initData ? tg.initData : '';
  const DEV = INIT ? '' : (hs.get('dev') || '');
  const DEMO = !INIT && !DEV && qs.get('demo') === '1';
  const app = document.getElementById('app');
  const view = document.getElementById('view');
  const tabbar = document.getElementById('tabbar');
  const live = document.getElementById('live');
  const HINT = 'Дата и время понимаются из текста: «позвонить в банк в пт в 10»';

  const reduced = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const tgv = (v) => { try { return !!(tg && tg.isVersionAtLeast && tg.isVersionAtLeast(v)); } catch (e) { return false; } };

  // ---------- DOM ----------
  function h(tag, props) {
    const el = document.createElement(tag);
    if (props) {
      for (const k in props) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'style') {
          for (const p in v) { if (p.startsWith('--')) el.style.setProperty(p, v[p]); else el.style[p] = v[p]; }
        } else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v === true ? '' : String(v));
      }
    }
    put(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }
  function put(el, kids) {
    for (const k of kids) {
      if (k == null || k === false) continue;
      if (Array.isArray(k)) put(el, k);
      else el.append(k.nodeType ? k : document.createTextNode(String(k)));
    }
  }
  const SVGNS = 'http://www.w3.org/2000/svg';
  function sv(tag, attrs) {
    const el = document.createElementNS(SVGNS, tag);
    for (const k in attrs || {}) el.setAttribute(k, attrs[k]);
    for (let i = 2; i < arguments.length; i++) if (arguments[i]) el.append(arguments[i]);
    return el;
  }
  const ICONS = {
    today: { d: [['c', 12, 12, 4], 'M12 2.5v2.5M12 19v2.5M2.5 12H5M19 12h2.5M5.3 5.3l1.8 1.8M16.9 16.9l1.8 1.8M5.3 18.7l1.8-1.8M16.9 7.1l1.8-1.8'] },
    journal: { d: ['M3 5h6a3 3 0 0 1 3 3v12a2 2 0 0 0-2-2H3z', 'M21 5h-6a3 3 0 0 0-3 3v12a2 2 0 0 1 2-2h7z'] },
    hero: { d: ['M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z'] },
    map: { d: ['M9 4L3 6v14l6-2 6 2 6-2V4l-6 2z', 'M9 4v14M15 6v14'] },
    replan: { w: 1.8, d: ['M20 11a8 8 0 1 0-2.3 5.7', 'M20 5v6h-6'] },
    plus: { w: 2.4, d: ['M12 5v14M5 12h14'] },
    sword: { w: 1.8, d: ['M14.5 3.5l6 6M17.5 6.5L7 17M5 15l4 4M3.5 20.5l2-2'] },
    more: { w: 2.4, d: [['c', 5, 12, 0.6], ['c', 12, 12, 0.6], ['c', 19, 12, 0.6]] },
    move: { w: 1.8, d: ['M4 12h14', 'M13 6l6 6-6 6'] },
    trash: { w: 1.8, d: ['M4 7h16', 'M9 7V4h6v3', 'M6 7l1 13h10l1-13'] },
  };
  function icon(name, size) {
    const P = ICONS[name];
    const el = sv('svg', { viewBox: '0 0 24 24', width: size || 22, height: size || 22, fill: 'none', stroke: 'currentColor',
      'stroke-width': P.w || 1.7, 'stroke-linecap': 'round', 'stroke-linejoin': 'round', 'aria-hidden': 'true', focusable: 'false' });
    for (const d of P.d) el.append(Array.isArray(d) ? sv('circle', { cx: d[1], cy: d[2], r: d[3] }) : sv('path', { d: d }));
    return el;
  }
  const corners = () => ['tl', 'tr', 'bl', 'br'].map((c) => h('span', { class: 'corner ' + c, 'aria-hidden': 'true' }));
  function secTitle(text, id, count, extra) {
    return h('div', { class: 'sec' + (extra ? ' ' + extra : '') },
      h('span', { class: 'dia', 'aria-hidden': 'true' }), h('h2', { id: id }, text),
      h('span', { class: 'rule', 'aria-hidden': 'true' }), count != null ? h('span', { class: 'count' }, count) : null);
  }

  // ---------- мелочи ----------
  const pad = (n) => String(n).padStart(2, '0');
  const fmt = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
  function plural(n, one, few, many) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (b === 1 && a !== 11) return one;
    if (b >= 2 && b <= 4 && (a < 12 || a > 14)) return few;
    return many;
  }
  const days = (n) => n + ' ' + plural(n, 'день', 'дня', 'дней');
  const WD = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  const WD_FULL = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'];
  const MON = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
  const parseD = (s) => { const p = s.split('-').map(Number); return new Date(p[0], p[1] - 1, p[2]); };
  const isoD = (d) => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  const addDays = (s, n) => { const d = parseD(s); d.setDate(d.getDate() + n); return isoD(d); };
  const wdIdx = (d) => (d.getDay() + 6) % 7;
  const shortDate = (s) => { const d = parseD(s); return WD[wdIdx(d)] + ', ' + d.getDate() + ' ' + MON[d.getMonth()]; };
  const dayMonth = (s) => { const d = parseD(s); return d.getDate() + ' ' + MON[d.getMonth()]; };
  const xpForLevel = (L) => 50 * L * (L + 1);
  function seasonName() {
    const d = new Date(), m = d.getMonth();
    return (m < 2 || m === 11 ? 'зима' : m < 5 ? 'весна' : m < 8 ? 'лето' : 'осень') + ' ' + d.getFullYear();
  }
  function localToday() { return isoD(new Date(Date.now() - 4 * 3600e3)); }
  const todayIso = () => (S.today ? S.today.date : localToday());

  function haptic(kind, arg) {
    try {
      const hf = tg && tgv('6.1') ? tg.HapticFeedback : null;
      if (!hf) return;
      if (kind === 'impact') hf.impactOccurred(arg || 'light');
      else if (kind === 'notify') hf.notificationOccurred(arg || 'success');
      else hf.selectionChanged();
    } catch (e) { /* старый клиент */ }
  }
  function announce(text) { live.textContent = ''; setTimeout(() => { live.textContent = text; }, 40); }
  let toastT = null;
  function toast(text, err) {
    const old = app.querySelector('.toast');
    if (old) old.remove();
    const t = h('div', { class: 'toast' + (err ? ' err' : ''), role: err ? 'alert' : 'status' }, text);
    app.append(t);
    clearTimeout(toastT);
    toastT = setTimeout(() => t.remove(), err ? 4200 : 2600);
  }

  // ---------- пиксельные спрайты (из макета) ----------
  function spr(map, pal, P) {
    const out = [];
    for (let r = 0; r < map.length; r++) {
      for (let c = 0; c < map[r].length; c++) {
        const col = pal[map[r].charAt(c)];
        if (col) out.push((c * P) + 'px ' + (r * P) + 'px 0 0 ' + col);
      }
    }
    return out.join(', ');
  }
  const LOW = ['.rRkbbbgbbbbkRr.', '.rRkttttttttkRr.', '.RRkTttttttTkRR.', '..RkppppppppkR..', '...kpppkkpppk...',
    '...kpppkkpppk...', '...kbbbkkbbbk...', '..kbbbbkkbbbbk..', '..kkkkkkkkkkkk..'];
  const IDLE = ['.....kkkkkk.....', '....khHHhhhk....', '...khHhhhhhhk...', '...khsssssshk...', '...kssessessk...',
    '...kSssssssSk...', '....kSssssSk....', '...rrkkSSkkrr...', '..rRtttggtttRr..', '..rRtTttttTtRr..',
    '.rRktTttttTtkRr.', '.rRsttttttttsRr.'].concat(LOW);
  const VICTORY = ['.....kkkkkk..w..', '....khHHhhhk.wW.', '...khHhhhhhhkwW.', '...khsssssshkwW.', '...kssessesskwW.',
    '...kSssssssSkwW.', '....kSssssSkggg.', '...rrkkSSkkr.s..', '..rRtttggtttRt..', '..rRtTttttTtt...',
    '.rRktTttttTtkRr.', '.rRsttttttttkRr.'].concat(LOW);
  const HPAL = { k: '#0B0D1C', h: '#6B3F22', H: '#9A6034', s: '#F0C29A', S: '#C98F6B', e: '#1B1530', r: '#9A3340', R: '#5E1D2A',
    t: '#3D5BA8', T: '#6C8BE0', g: '#E8C170', b: '#5A3A22', p: '#2B2F4A', w: '#E6ECF7', W: '#9AA3BC' };
  const FIRE_A = ['....y.....', '...yo..y..', '...oo..o..', '..oryo.o..', '..oryyroo.', '.orYYYyro.', '.orYYYYro.', '..rrrrrr..', '.bBbbbbBb.', 'bBbbbbbbBb'];
  const FIRE_B = ['......y...', '..y..oy...', '..o..oo...', '..o.oyro..', '.oorYyro..', '.oryYYYro.', '.orYYYYro.', '..rrrrrr..', '.bBbbbbBb.', 'bBbbbbbbBb'];
  const FPAL = { y: '#FFF1B8', Y: '#FFD27A', o: '#FF9F43', r: '#D9502E', b: '#4A2F1C', B: '#7A5232' };
  const CHEST = ['.kkkkkkkkkk.', 'kbbbbbbbbbbk', 'kbBbbbbbbbbk', 'kggggggggggk', 'kbbbbyybbbbk', 'kbbbbyybbbbk', 'kbbbbbbbbbbk', 'kbbbbbbbbbbk', 'kggggggggggk', '.kkkkkkkkkk.'];
  const CPAL = { k: '#0B0D1C', b: '#7A4B2A', B: '#A0663A', g: '#E8C170', y: '#FFF1B8' };
  const BADGES = {
    flame: [['...r....', '..rr.r..', '..rrorr.', '.rroorr.', '.rooyor.', '.royyor.', '..ryyr..', '...rr...'], { r: '#FF7B5F', o: '#FFB067', y: '#FFF1B8' }],
    heart: [['.rr..rr.', 'rrrrrrrr', 'rwrrrrrr', 'rrrrrrrr', '.rrrrrr.', '..rrrr..', '...rr...', '........'], { r: '#8FE0BC', w: '#E2FFF2' }],
    star: [['...gg...', '...gg...', 'gggggggg', '.gggggg.', '..gggg..', '.gg..gg.', 'gg....gg', '........'], { g: '#F5D58A' }],
    lock: [['..aaaa..', '.a....a.', '.a....a.', 'aaaaaaaa', 'aaa..aaa', 'aaa..aaa', 'aaaaaaaa', '........'], { a: '#5E6596' }],
  };
  const SPARK_DIRS = [[-40, -30], [0, -48], [40, -30], [-50, 4], [50, 4], [-28, 30], [28, 30], [0, -24]];
  const SPARK_COLS = ['#F5D58A', '#FFF1B8', '#F5D58A', '#FFB067', '#FFF1B8', '#F5D58A', '#FFB067', '#FFF1B8'];

  // ---------- сцена у костра ----------
  const SCENES = {
    small: {
      H: 176, P: 4, hero: [150, 22, 64, 84], eyes: [[24, 16], [36, 16]], fire: [246, 22, 4, 40], glow: [166, -44, 200, 150],
      shadow: [150, 16, 64, 10], emberBottom: 54,
      embers: [[262, 'hd-e0 2.6s linear 0s infinite'], [268, 'hd-e1 3.1s linear 0.8s infinite'], [266, 'hd-e2 2.3s linear 1.4s infinite'],
        [270, 'hd-e0 2.9s linear 2s infinite'], [264, 'hd-e1 2.5s linear 0.4s infinite'], [268, 'hd-e2 3.3s linear 1.8s infinite']],
      ground: 36, moon: [332, 22, 22, '0 0 24px 8px rgba(243, 231, 198, 0.28)'],
      stars: [[40, 70, 0], [96, 18, 0.6], [150, 52, 1.2], [214, 26, 0.3], [290, 58, 1.7], [318, 14, 0.9], [370, 62, 2.1]],
      pineBase: 142, pines: [[14, 0.9], [44, 1.2], [300, 1.1], [332, 0.8], [356, 1.3]],
      mount: '0,122 36,96 64,108 104,72 146,104 186,84 226,110 268,76 308,98 348,84 390,104 390,176 0,176', mountBlur: 1.6, pineBlur: 0.7,
      fg: 'M0 30 L0 16 L10 8 L16 18 L26 4 L34 18 L48 10 L56 20 L70 12 L80 22 L300 22 L312 10 L320 20 L334 6 L342 18 L356 8 L366 18 L378 10 L390 16 L390 30 Z',
      fgH: 30, fgBlur: 2.4, idle: 'hd-idle 1.6s steps(2) infinite', jump: 'hd-jump 0.7s steps(7) 1', spark: [180, 100], float: [112, 76],
    },
    big: {
      H: 236, P: 6, hero: [104, 30, 96, 126], eyes: [[36, 24], [54, 24]], fire: [254, 30, 5, 50], glow: [150, -60, 260, 200],
      shadow: [104, 22, 96, 12], emberBottom: 72,
      embers: [[276, 'hd-E0 2.8s linear 0s infinite'], [282, 'hd-E1 3.2s linear 0.8s infinite'], [280, 'hd-E2 2.4s linear 1.4s infinite'],
        [284, 'hd-E0 3s linear 2s infinite'], [278, 'hd-E1 2.6s linear 0.4s infinite'], [282, 'hd-E2 3.4s linear 1.8s infinite']],
      ground: 44, moon: [44, 26, 28, '0 0 30px 10px rgba(243, 231, 198, 0.26)'],
      stars: [[120, 30, 0], [176, 70, 0.6], [214, 22, 1.2], [262, 64, 0.3], [300, 40, 1.7], [350, 76, 0.9], [20, 90, 2.1], [88, 76, 1.5]],
      pineBase: 192, pines: [[10, 1.1], [52, 1.5], [214, 0.9], [356, 1.4]],
      mount: '0,150 40,118 76,132 118,92 160,126 200,104 240,134 284,96 326,120 362,104 390,118 390,236 0,236', mountBlur: 1.8, pineBlur: 0.8,
      fg: 'M0 34 L0 18 L12 8 L18 20 L30 4 L38 20 L52 10 L60 22 L74 14 L84 26 L296 26 L308 12 L318 22 L332 6 L340 20 L354 8 L366 20 L378 12 L390 18 L390 34 Z',
      fgH: 34, fgBlur: 2.6, idle: 'hd-idleB 1.6s steps(2) infinite', jump: 'hd-jumpB 0.7s steps(7) 1', spark: [152, 118], float: [82, 96], chest: [324, 30],
    },
  };
  function pinesPath(base, list) {
    let d = '';
    for (const p of list) {
      const x = p[0], u = 6 * p[1], b = base;
      d += 'M' + x + ' ' + b + 'L' + (x + u) + ' ' + (b - 2 * u) + 'L' + (x + 0.5 * u) + ' ' + (b - 2 * u) + 'L' + (x + 1.5 * u) + ' ' + (b - 4 * u) +
        'L' + (x + u) + ' ' + (b - 4 * u) + 'L' + (x + 2 * u) + ' ' + (b - 6 * u) + 'L' + (x + 3 * u) + ' ' + (b - 4 * u) + 'L' + (x + 2.5 * u) + ' ' + (b - 4 * u) +
        'L' + (x + 3.5 * u) + ' ' + (b - 2 * u) + 'L' + (x + 3 * u) + ' ' + (b - 2 * u) + 'L' + (x + 4 * u) + ' ' + b + 'Z';
    }
    return d;
  }
  const box = (l, b, w, hh, extra) => Object.assign({ left: l + 'px', bottom: b + 'px', width: w + 'px', height: hh + 'px' }, extra || {});

  function buildScene(kind, opts) {
    const C = SCENES[kind];
    const el = h('div', { class: 'scene ' + kind });
    const stage = () => h('div', { class: 'stage', 'aria-hidden': 'true' });

    const back = stage();
    for (const st of C.stars) {
      back.append(h('span', { style: { left: st[0] + 'px', top: st[1] + 'px', width: '2px', height: '2px', background: '#F3E7C6',
        animation: 'hd-twinkle 2.6s ease-in-out ' + st[2] + 's infinite' } }));
    }
    back.append(h('span', { style: { left: C.moon[0] + 'px', top: C.moon[1] + 'px', width: C.moon[2] + 'px', height: C.moon[2] + 'px',
      borderRadius: '50%', background: '#F3E7C6', boxShadow: C.moon[3] } }));
    el.append(back);

    const mount = sv('svg', { viewBox: '0 0 390 ' + C.H, preserveAspectRatio: 'none', 'aria-hidden': 'true', class: 'full' },
      sv('polygon', { points: C.mount, fill: '#1C2352' }));
    mount.style.bottom = '0'; mount.style.height = C.H + 'px'; mount.style.filter = 'blur(' + C.mountBlur + 'px)';
    el.append(mount);

    const mid = stage();
    const pines = sv('svg', { viewBox: '0 0 390 ' + C.H, width: '390', height: String(C.H), 'shape-rendering': 'crispEdges' },
      sv('path', { d: pinesPath(C.pineBase, C.pines), fill: '#121738' }));
    pines.style.position = 'absolute'; pines.style.left = '0'; pines.style.top = '0'; pines.style.filter = 'blur(' + C.pineBlur + 'px)';
    mid.append(pines);
    el.append(mid);

    el.append(h('span', { class: 'full', 'aria-hidden': 'true', style: { bottom: '0', height: C.ground + 'px', background: 'linear-gradient(180deg, #1C2246, #0C0F27)' } }));
    el.append(h('span', { class: 'full', 'aria-hidden': 'true', style: { bottom: C.ground + 'px', height: '4px', background: 'repeating-linear-gradient(90deg, #1C2246 0 8px, transparent 8px 20px)' } }));

    const front = h('div', { class: 'stage' });
    front.append(h('span', { 'aria-hidden': 'true', style: box(C.glow[0], C.glow[1], C.glow[2], C.glow[3], {
      background: 'radial-gradient(ellipse at center, rgba(255, 170, 80, 0.5), rgba(255, 140, 60, 0.14) 45%, transparent 70%)',
      animation: 'hd-glow 1.6s ease-in-out infinite alternate' }) }));
    front.append(h('span', { 'aria-hidden': 'true', style: box(C.shadow[0], C.shadow[1], C.shadow[2], C.shadow[3], {
      borderRadius: '50%', background: 'radial-gradient(ellipse at center, rgba(0, 0, 0, 0.55), transparent 70%)' }) }));
    const F = C.fire;
    const fireFrame = (map, cls, anim) => h('span', { class: cls, 'aria-hidden': 'true', style: box(F[0], F[1], F[3], F[3], { animation: anim, opacity: cls === 'fire-b' ? '0' : '1' }) },
      h('span', { class: 'px', style: { width: F[2] + 'px', height: F[2] + 'px', boxShadow: spr(map, FPAL, F[2]) } }));
    front.append(fireFrame(FIRE_A, 'fire-a', 'hd-fa 0.5s steps(1) infinite'), fireFrame(FIRE_B, 'fire-b', 'hd-fb 0.5s steps(1) infinite'));
    for (const em of C.embers) {
      front.append(h('span', { class: 'ember', 'aria-hidden': 'true', style: { left: em[0] + 'px', bottom: C.emberBottom + 'px', width: '3px', height: '3px',
        background: '#FFC070', boxShadow: '0 0 5px #FF9F43', opacity: '0', animation: em[1] } }));
    }
    if (C.chest) {
      front.append(h('span', { class: 'chest', 'aria-hidden': 'true', style: box(C.chest[0], C.chest[1], 48, 40) },
        h('span', { class: 'px', style: { width: '4px', height: '4px', boxShadow: spr(CHEST, CPAL, 4) } }), h('span', { class: 'glint' })));
    }
    const P = C.P;
    const sprite = h('span', { class: 'px', 'aria-hidden': 'true', style: { width: P + 'px', height: P + 'px', boxShadow: spr(IDLE, HPAL, P) } });
    const hero = h('button', { type: 'button', class: 'hero', 'aria-label': opts.label, onclick: opts.onHero,
      style: box(C.hero[0], C.hero[1], C.hero[2], C.hero[3], { animation: C.idle }) }, sprite,
      C.eyes.map((e) => h('span', { class: 'eye', 'aria-hidden': 'true', style: { left: e[0] + 'px', top: e[1] + 'px', width: P + 'px', height: P + 'px' } })));
    front.append(hero);
    el.append(front);

    const fg = sv('svg', { viewBox: '0 0 390 ' + C.fgH, preserveAspectRatio: 'none', 'aria-hidden': 'true', class: 'full' }, sv('path', { d: C.fg, fill: '#04060F' }));
    fg.style.bottom = '-6px'; fg.style.height = C.fgH + 'px'; fg.style.filter = 'blur(' + C.fgBlur + 'px)';
    el.append(fg, h('span', { class: 'vignette', 'aria-hidden': 'true' }));
    return { el: el, stage: front, hero: hero, sprite: sprite, C: C, t: null };
  }

  function heroCheer(sc, ms) {
    sc.sprite.style.boxShadow = spr(VICTORY, HPAL, sc.C.P);
    sc.hero.style.animation = 'none';
    void sc.hero.offsetWidth;
    sc.hero.style.animation = sc.C.jump;
    clearTimeout(sc.t);
    sc.t = setTimeout(() => {
      sc.sprite.style.boxShadow = spr(IDLE, HPAL, sc.C.P);
      sc.hero.style.animation = sc.C.idle;
    }, ms || 1600);
  }
  function sparks(sc) {
    if (reduced()) return;
    SPARK_DIRS.forEach((d, i) => {
      const sp = h('span', { class: 'spark', 'aria-hidden': 'true', style: { left: sc.C.spark[0] + 'px', top: sc.C.spark[1] + 'px',
        background: SPARK_COLS[i], boxShadow: '0 0 6px ' + SPARK_COLS[i], '--dx': d[0] + 'px', '--dy': d[1] + 'px' } });
      sc.stage.append(sp);
      setTimeout(() => sp.remove(), 900);
    });
  }
  function floatXp(sc, text) {
    const f = h('span', { class: 'xpfloat', 'aria-hidden': 'true', style: { left: sc.C.float[0] + 'px', top: sc.C.float[1] + 'px' } }, text);
    sc.stage.append(f);
    setTimeout(() => f.remove(), 1300);
  }
  function ribbon(sc, big, small) {
    const old = sc.el.querySelector('.ribbon');
    if (old) old.remove();
    const r = h('div', { class: 'ribbon', 'aria-hidden': 'true' }, h('span', { class: 'shine' }), h('div', { class: 'big' }, big),
      small ? h('div', { class: 'small' }, small) : null);
    sc.el.append(r);
    setTimeout(() => r.remove(), 1700);
  }
  function levelUp(from, to) {
    haptic('notify', 'success');
    S.fxUntil = Math.max(S.fxUntil || 0, Date.now() + 3000);
    const ov = h('div', { class: 'lvlup' }, h('div', { class: 'dim' }), h('div', { class: 'pillar', 'aria-hidden': 'true' }),
      h('div', { class: 'box', role: 'status' },
        h('span', { class: 'who' }, h('span', { 'aria-hidden': 'true' }, '◆ '), 'Георгий', h('span', { 'aria-hidden': 'true' }, ' ◆')),
        h('span', { class: 'head' }, 'Уровень повышен'), h('span', { class: 'lv' }, from + ' → ' + to),
        h('span', { class: 'note' }, 'Сундук и награды ждут в «Персонаже»')));
    // оверлей не ловит нажатия (pointer-events: none): под ним можно дальше отмечать
    app.append(ov);
    setTimeout(() => ov.remove(), 2900);
  }

  // ---------- состояние и API ----------
  const S = { tab: 'today', today: null, list: { week: null, later: null }, view: 'week', character: null, grid: null,
    sel: null, talk: 0, pending: new Set(), fxUntil: 0, rateDraft: null };
  let T = null;              // живые ссылки экрана «Сегодня»
  let closeSheet = null;

  async function api(method, path, body) {
    if (DEMO) return Demo.handle(method, path, body);
    const res = await fetch(path, {
      method: method, cache: 'no-store',
      headers: { 'Content-Type': 'application/json', Authorization: INIT ? 'tma ' + INIT : 'dev ' + DEV },
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && (data.detail || data.error)) || ('HTTP ' + res.status));
      err.status = res.status;
      throw err;
    }
    return data;
  }
  const KEYS = { today: 'today', journal: 'list', character: 'character', map: 'grid' };
  async function load(tab) {
    if (tab === 'today') S.today = await api('GET', 'api/today');
    else if (tab === 'journal') S.list[S.view] = await api('GET', 'api/list?view=' + S.view);
    else if (tab === 'character') S.character = await api('GET', 'api/character');
    else if (tab === 'map') S.grid = await api('GET', 'api/grid');
  }
  const hasData = (tab) => tab === 'journal' || !!S[KEYS[tab]];

  function handleErr(e) {
    if (e && e.status === 401) {
      tabbar.hidden = true;
      fab.hidden = true;
      view.replaceChildren(gate('Нет доступа', 'Telegram не подтвердил вход. Закрой приложение и открой его заново из бота.', false));
      return;
    }
    const offline = e instanceof TypeError || !e.status;
    toast(offline ? 'Нет связи с сервером. Попробуй ещё раз.' : 'Не вышло: ' + e.message, true);
    if (!hasData(S.tab)) view.replaceChildren(gate('Костёр не разгорелся', 'Сервер не ответил. Попробуй ещё раз.', true));
  }

  // ---------- навигация ----------
  const TABS = [['today', 'Сегодня', 'today'], ['journal', 'Журнал', 'journal'], ['character', 'Персонаж', 'hero'], ['map', 'Карта', 'map']];
  const fab = h('button', { type: 'button', class: 'fab', 'aria-label': 'Добавить квест', hidden: true, onclick: () => openAdd('today') }, icon('plus', 22));

  function paintTabs() {
    tabbar.querySelectorAll('.tab').forEach((b) => {
      if (b.dataset.tab === S.tab) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    fab.hidden = S.tab !== 'today' || !S.today;
  }
  const SCREENS = { today: screenToday, journal: screenJournal, character: screenCharacter, map: screenMap };
  function render(keepScroll) {
    const top = view.scrollTop;
    paintTabs();
    if (S.tab !== 'today') T = null;
    view.replaceChildren(hasData(S.tab) ? SCREENS[S.tab]() : h('div', { class: 'loading', role: 'status' }, 'Разжигаем костёр…'));
    if (keepScroll) view.scrollTop = top;
  }
  async function show(tab) {
    const switched = S.tab !== tab;
    S.tab = tab;
    render(!switched);
    if (switched) view.scrollTop = 0;
    try {
      await load(tab);
      if (S.tab === tab) render(true);
    } catch (e) { handleErr(e); }
  }
  async function refreshTab() {
    const tab = S.tab;
    try {
      await load(tab);
      if (S.tab === tab && !closeSheet) render(true);
    } catch (e) { handleErr(e); }
  }
  let refreshT = null;
  function scheduleRefresh(delay) {
    clearTimeout(refreshT);
    refreshT = setTimeout(function tick() {
      const wait = (S.fxUntil || 0) - Date.now();
      if (wait > 0) { refreshT = setTimeout(tick, wait + 60); return; }
      S.character = null; S.grid = null; S.list = { week: null, later: null };
      if (S.tab === 'today') refreshTab();
      else api('GET', 'api/today').then((d) => { S.today = d; }).catch(() => {});
    }, delay || 1800);
  }

  // ---------- «Сегодня» ----------
  function screenToday() {
    const d = S.today;
    const sc = buildScene('small', { label: 'Персонаж, уровень ' + d.character.level, onHero: () => show('character') });
    sc.el.append(h('div', { class: 'scene-title' }, h('span', { class: 'kicker' }, 'Неделя ' + d.week), h('h1', null, d.date_label)));
    const st = statusPanel();
    const habits = h('div', { class: 'habits', role: 'group', 'aria-label': 'Ритуалы дня' });
    const quests = h('section', { class: 'quests', 'aria-labelledby': 'q-title' });
    const rate = d.rating ? ratePanel(d.rating) : null;
    T = { sc: sc, st: st, habits: habits, quests: quests, rate: rate };
    paintStatus(d.character, d.summary, false);
    paintHabits();
    paintQuests();
    return h('div', { class: 'screen' }, sc.el, st.el, d.habits.length ? habits : null, rate ? rate.el : null, quests);
  }

  // ---------- оценка дня: ползунок -10..+10, пишет rpg.log_rating (как /day в боте) ----------
  const MON_NOM = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];
  const signed = (n) => {
    if (n == null) return '–';
    const v = Number(n);
    if (v === 0) return '0';
    const s = Number.isInteger(v) ? String(Math.abs(v)) : Math.abs(v).toFixed(1).replace('.', ',');
    return (v > 0 ? '+' : '−') + s;
  };
  function ratePanel(rt) {
    // недосохранённое значение переживает фоновое обновление экрана
    const draft = S.rateDraft && S.rateDraft.day === rt.day ? S.rateDraft.value : null;
    const val = h('output', { class: 'rv', for: 'rate-in' });
    const input = h('input', { id: 'rate-in', type: 'range', min: '-10', max: '10', step: '1',
      value: String(draft != null ? draft : rt.score != null ? rt.score : 0) });
    const save = h('button', { type: 'button', class: 'btn-gold rsave' });
    const month = h('span', { class: 'rm' });
    const el = h('section', { class: 'rate', 'aria-labelledby': 'rate-title' },
      h('div', { class: 'rhead' }, h('span', { class: 'dia', 'aria-hidden': 'true' }), h('h2', { id: 'rate-title' }, 'Оценка дня'), val),
      h('div', { class: 'rtrack' }, input),
      h('div', { class: 'rticks', 'aria-hidden': 'true' }, h('span', null, '−10'), h('span', null, '0'), h('span', null, '+10')),
      h('div', { class: 'rfoot' }, month, save));
    const R = { el: el, input: input, val: val, save: save, month: month, busy: false };
    const paint = () => {
      const r = S.today.rating;
      const v = Number(input.value);
      const dirty = r.score == null || v !== r.score;
      const touched = !!(S.rateDraft && S.rateDraft.day === r.day);
      const blank = r.score == null && !touched;          // ещё не оценён и ползунок не трогали
      val.textContent = blank ? '–' : signed(v);
      val.className = 'rv' + (blank ? ' blank' : v > 0 ? ' up' : v < 0 ? ' down' : '');
      const p = (v + 10) * 5;
      const lo = Math.min(50, p), hi = Math.max(50, p);
      const col = v > 0 ? 'var(--jade)' : v < 0 ? 'var(--rose)' : 'var(--gold)';
      input.style.setProperty('--trk', 'linear-gradient(90deg, #1A2050 ' + lo + '%, ' + col + ' ' + lo + '%, ' + col + ' ' + hi + '%, #1A2050 ' + hi + '%)');
      input.setAttribute('aria-valuetext', signed(v) + (r.score == null ? ', день ещё не оценён' : dirty ? ', не сохранено' : ', записано'));
      input.setAttribute('aria-label', 'Оценка дня от минус 10 до плюс 10');
      save.textContent = R.busy ? 'Пишу…' : r.score == null ? 'Записать' : dirty ? 'Обновить' : 'Записано';
      save.disabled = R.busy || !dirty;
      const mname = MON_NOM[Number(r.day.slice(5, 7)) - 1];
      month.textContent = r.month_days
        ? mname.replace(/^./, (s) => s.toUpperCase()) + ': в среднем ' + signed(r.month_avg) + ' · ' + r.month_days + ' дн.'
        : 'Первая оценка в этом месяце';
      if (r.note && !dirty) month.textContent += ' · ' + r.note;
    };
    R.paint = paint;
    input.addEventListener('input', () => {
      S.rateDraft = { day: S.today.rating.day, value: Number(input.value) };
      S.fxUntil = Math.max(S.fxUntil || 0, Date.now() + 4000);      // пока тянешь, экран не перерисуется
      haptic('select');
      paint();
    });
    save.addEventListener('click', () => saveRating(R));
    paint();
    return R;
  }
  async function saveRating(R) {
    if (R.busy) return;
    const score = Number(R.input.value);
    R.busy = true;
    R.paint();
    try {
      const r = await api('POST', 'api/rating', { score: score });
      S.rateDraft = null;
      const before = S.today.character;
      S.today.rating = r.rating;
      applyCharacter(r.character);
      S.today.summary.xp_today += r.xp || 0;
      if (r.xp > 0) S.today.habits.forEach((x) => { if (x.via === 'rating') x.done = true; });
      if (T && T.rate === R) {
        paintStatus(S.today.character, S.today.summary, !!r.level_up);
        paintHabits();
      }
      const msg = 'Оценка дня ' + signed(score) + (r.xp > 0 ? ', +' + r.xp + ' опыта' : '');
      announce(msg);
      if (r.xp > 0 && T) {
        haptic('notify', 'success');
        S.fxUntil = Date.now() + (r.milestone || r.level_up ? 3400 : 1400);
        heroCheer(T.sc, 1200);
        floatXp(T.sc, '+' + r.xp + ' XP');
      } else {
        haptic('impact', 'light');
        toast('Записал: ' + signed(score));
      }
      if (r.milestone && T) {
        sparks(T.sc);
        ribbon(T.sc, 'Этап пройден', r.milestone.title + ' · +' + r.milestone.xp);
      }
      if (r.level_up) setTimeout(() => levelUp(before.level, r.character.level), r.milestone ? 1200 : 300);
      scheduleRefresh();
    } catch (e) {
      handleErr(e);
    } finally {
      R.busy = false;
      R.paint();
    }
  }
  function focusRating() {
    if (!T || !T.rate) return;
    T.rate.el.scrollIntoView({ behavior: reduced() ? 'auto' : 'smooth', block: 'center' });
    T.rate.input.focus({ preventScroll: true });
  }
  function statusPanel() {
    const lvl = h('span');
    const name = h('span', { class: 'name' });
    const xpt = h('span', { class: 'xptext' });
    const fill = h('i');
    const bar = h('div', { class: 'bar', role: 'progressbar', 'aria-label': 'Опыт до следующего уровня', 'aria-valuemin': 0 }, fill);
    const sub = h('span', { class: 'sub' });
    const btn = h('button', { type: 'button', class: 'iconbtn', 'aria-label': 'Пересобрать день', title: 'Пересобрать день', onclick: replan }, icon('replan', 20));
    const el = h('div', { class: 'panel status' }, corners(), h('div', { class: 'lvl', 'aria-hidden': 'true' }, lvl),
      h('div', { class: 'stack' }, h('div', { class: 'line' }, name, xpt), bar, sub), btn);
    return { el: el, lvl: lvl, name: name, xpt: xpt, fill: fill, bar: bar, sub: sub, btn: btn };
  }
  function pctOf(c) {
    const span = c.next_level_at - c.level_floor;
    return span > 0 ? Math.max(0, Math.min(100, (c.xp - c.level_floor) * 100 / span)) : 0;
  }
  function paintStatus(c, sum, levelled) {
    if (!T) return;
    const r = T.st;
    r.lvl.textContent = c.level;
    r.name.textContent = 'Георгий · ур. ' + c.level;
    r.xpt.textContent = fmt(c.xp) + ' / ' + fmt(c.next_level_at);
    r.bar.setAttribute('aria-valuemax', c.next_level_at - c.level_floor);
    r.bar.setAttribute('aria-valuenow', c.xp - c.level_floor);
    const pct = pctOf(c);
    if (levelled && !reduced()) {
      r.fill.style.width = '100%';
      setTimeout(() => {
        r.fill.style.transition = 'none';
        r.fill.style.width = '0%';
        void r.fill.offsetWidth;
        r.fill.style.transition = '';
        r.fill.style.width = pct + '%';
      }, 850);
    } else {
      r.fill.style.width = pct + '%';
    }
    r.sub.textContent = 'Квесты ' + sum.done + ' из ' + sum.total + ' · сегодня +' + sum.xp_today + ' опыта';
  }
  function paintHabits() {
    T.habits.replaceChildren.apply(T.habits, S.today.habits.map((hb) => {
      const b = h('button', { type: 'button', class: 'habit' + (hb.done ? ' on' : '') + (hb.auto ? ' auto' : ''),
        'aria-pressed': String(!!hb.done), 'aria-disabled': hb.auto && hb.via !== 'rating' ? 'true' : null },
      h('span', { class: 'gem', 'aria-hidden': 'true' }),
      h('span', { class: 'txt' }, h('span', { class: 'hn' }, hb.title), h('span', { class: 'hs' }, hb.note || '')));
      const label = () => b.setAttribute('aria-label', (hb.via === 'rating' ? 'Ритуал отмечается оценкой дня, перейти к ползунку' + (hb.done ? ', выполнен: ' : ': ')
        : hb.auto ? 'Ритуал отмечается сам' + (hb.done ? ', выполнен: ' : ': ')
        : hb.done ? 'Ритуал выполнен, снять отметку: ' : 'Отметить ритуал: ') + hb.title);
      label();
      b.addEventListener('click', () => toggleHabit(hb, b, label));
      return b;
    }));
  }
  function paintQuests() {
    const d = S.today;
    const kids = [secTitle('Квесты дня', 'q-title')];
    for (const it of d.timeline) kids.push(timelineRow(it));
    if (!d.timeline.some((x) => x.type === 'task') && !d.unscheduled.length) {
      kids.push(h('p', { class: 'empty' }, 'Квестов на сегодня нет. Новый добавляется золотым ромбом внизу.'));
    }
    if (d.unscheduled.length) {
      kids.push(secTitle('Без времени', null, String(d.unscheduled.length), 'later'));
      for (const t of d.unscheduled) kids.push(taskCard(t, 'today'));
    }
    T.quests.replaceChildren.apply(T.quests, kids);
  }
  function timelineRow(it) {
    if (it.type === 'now') {
      return h('div', { class: 'nowrow' }, h('span', { class: 't' }, h('span', { class: 'sr' }, 'Сейчас '), it.time),
        h('span', { class: 'nd', 'aria-hidden': 'true' }), h('span', { class: 'nl', 'aria-hidden': 'true' }));
    }
    let body;
    if (it.type === 'task') body = taskCard(it, 'today');
    else if (it.type === 'meeting') {
      body = h('div', { class: 'meet' }, h('span', { class: 'dot', 'aria-hidden': 'true', style: { background: it.color } }),
        h('div', { class: 'col' }, h('span', { class: 'mt' }, it.title), h('span', { class: 'sub' }, it.meta)));
    } else if (it.type === 'gap') body = h('div', { class: 'gapbox' }, it.title);
    else if (it.type === 'training') {
      const sw = icon('sword', 20);
      sw.style.color = '#8FE0BC';
      sw.style.flex = 'none';
      body = h('div', { class: 'train' + (it.done ? ' done' : '') }, sw,
        h('div', { class: 'col' }, h('span', { class: 'tt' }, it.title + (it.done ? ' · сделано' : '')), h('span', { class: 'tm' }, it.meta)));
    } else return null;
    return h('div', { class: 'row' + (it.type === 'gap' ? ' compact' : '') }, h('div', { class: 't' }, it.time || ''), body);
  }

  function metaLine(t, where) {
    const parts = [];
    if (where === 'list' && t.time) parts.push(t.time);
    if (t.context_title) parts.push(t.context_title);
    parts.push(t.size);
    if (t.cell_title) parts.push(t.cell_title);
    return h('span', { class: 'meta' }, h('span', { class: 'dot', 'aria-hidden': 'true', style: { background: t.color } }),
      h('span', null, t.overdue && t.due ? h('span', { class: 'late' }, 'просрочено с ' + dayMonth(t.due) + ' · ') : null, parts.join(' · ')));
  }
  function taskCard(t, where) {
    const gem = h('span', { class: 'gem', 'aria-hidden': 'true' });
    const check = h('button', { type: 'button', class: 'check' }, gem);
    const card = h('div', { class: 'card' + (t.done ? ' done' : '') + (t.overdue ? ' overdue' : ''), 'data-id': t.id });
    const labels = () => {
      check.setAttribute('aria-pressed', String(!!t.done));
      check.setAttribute('aria-label', (t.done ? 'Снять отметку: ' : 'Отметить выполненным: ') + t.title);
    };
    labels();
    check.addEventListener('click', () => toggleTask(t, card, gem, labels, where));
    const more = h('button', { type: 'button', class: 'more', 'aria-label': 'Действия: ' + t.title, onclick: () => taskMenu(t) }, icon('more', 20));
    card.append(check, h('div', { class: 'body' }, h('span', { class: 'title' }, t.title), metaLine(t, where)),
      h('span', { class: 'xp', 'aria-label': t.xp + ' опыта' }, '+' + t.xp), more);
    longPress(card, () => taskMenu(t));
    return card;
  }
  function longPress(el, fn) {
    let timer = null, x = 0, y = 0, fired = false;
    const cancel = () => clearTimeout(timer);
    el.addEventListener('pointerdown', (e) => {
      if (e.button) return;
      fired = false; x = e.clientX; y = e.clientY;
      clearTimeout(timer);
      timer = setTimeout(() => { fired = true; haptic('impact', 'medium'); fn(); }, 520);
    });
    el.addEventListener('pointermove', (e) => { if (Math.abs(e.clientX - x) > 10 || Math.abs(e.clientY - y) > 10) cancel(); });
    el.addEventListener('pointerup', cancel);
    el.addEventListener('pointercancel', cancel);
    el.addEventListener('pointerleave', cancel);
    el.addEventListener('contextmenu', (e) => { e.preventDefault(); if (!fired) { fired = true; fn(); } });
    el.addEventListener('click', (e) => { if (fired) { e.stopPropagation(); e.preventDefault(); fired = false; } }, true);
  }

  function applyCharacter(c) {
    if (S.today) S.today.character = c;
    if (S.character) Object.assign(S.character, c);
  }
  async function toggleTask(t, card, gem, labels, where) {
    if (S.pending.has(t.id)) return;
    S.pending.add(t.id);
    const was = !!t.done;
    t.done = !was;
    card.classList.toggle('done', t.done);
    labels();
    if (!was) {
      haptic('notify', 'success');
      if (!reduced()) {
        gem.classList.remove('pop'); void gem.offsetWidth; gem.classList.add('pop');
        const sw = h('span', { class: 'sweep', 'aria-hidden': 'true' });
        card.append(sw);
        setTimeout(() => sw.remove(), 1000);
      }
    } else haptic('impact', 'light');
    try {
      const r = await api('POST', 'api/tasks/' + t.id + '/' + (was ? 'undone' : 'done'));
      Object.assign(t, r.task);
      const before = S.today ? S.today.character : null;
      applyCharacter(r.character);
      if (S.today) {
        S.today.summary.done = Math.max(0, S.today.summary.done + (was ? -1 : 1));
        if (!was) S.today.summary.xp_today += r.xp || 0;
        if (S.tab === 'today') paintStatus(S.today.character, S.today.summary, !!r.level_up);
      }
      if (!was) celebrate(t, r, before);
      if (where === 'list' && !was) {
        card.classList.add('leaving');
        setTimeout(() => { if (S.tab === 'journal') refreshTab(); }, 900);
      }
      scheduleRefresh();
    } catch (e) {
      t.done = was;
      card.classList.toggle('done', was);
      labels();
      handleErr(e);
    } finally {
      S.pending.delete(t.id);
    }
  }
  function celebrate(t, r, before) {
    const note = r.xp > 0 ? '+' + r.xp + ' опыта' : t.cell_id ? 'потолок опыта на сегодня' : 'опыт придёт, когда квест получит клетку';
    announce('Квест выполнен: ' + t.title + '. ' + note);
    S.fxUntil = Date.now() + (r.level_up ? 3400 : 1800);
    if (S.tab === 'today' && T) {
      heroCheer(T.sc, 1600);
      sparks(T.sc);
      if (r.xp > 0) floatXp(T.sc, '+' + r.xp + ' XP');
      ribbon(T.sc, 'Квест выполнен', r.xp > 0 ? t.title : t.title + ' · ' + note);
    } else {
      toast('Квест выполнен · ' + note);
    }
    if (r.level_up) {
      const from = before ? before.level : r.character.level - 1;
      setTimeout(() => levelUp(from, r.character.level), S.tab === 'today' ? 1000 : 300);
    }
  }
  async function toggleHabit(hb, b, label) {
    if (hb.via === 'rating') { toast('Отмечается оценкой дня: ползунок ниже'); focusRating(); return; }
    if (hb.auto) { toast(hb.via === 'garmin' ? 'Этот ритуал отмечается сам, из Garmin' : 'Этот ритуал отмечается сам'); return; }
    const key = 'h:' + hb.cell_id;
    if (S.pending.has(key)) return;
    S.pending.add(key);
    const was = !!hb.done;
    const set = (v) => { hb.done = v; b.classList.toggle('on', v); b.setAttribute('aria-pressed', String(v)); label(); };
    set(!was);
    haptic('impact', 'light');
    try {
      const r = await api('POST', 'api/habits/' + encodeURIComponent(hb.cell_id) + '/toggle');
      set(!!r.done);
      const before = S.today.character;
      applyCharacter(r.character);
      if (r.xp > 0) S.today.summary.xp_today += r.xp;
      paintStatus(S.today.character, S.today.summary, !!r.level_up);
      if (r.done && r.xp > 0 && T) {
        heroCheer(T.sc, 1200);
        floatXp(T.sc, '+' + r.xp + ' XP');
        haptic('notify', 'success');
        announce('Ритуал отмечен: ' + hb.title + ', +' + r.xp + ' опыта');
        S.fxUntil = Date.now() + 1400;
      }
      if (r.locked) toast('Уже отмечено задачей или из Garmin, здесь не снимается');
      else if (!was && r.done && !(r.xp > 0)) toast('Отмечено. Опыта сегодня больше не будет: по этой характеристике уже 100');
      if (r.level_up) levelUp(before.level, r.character.level);
      scheduleRefresh();
    } catch (e) {
      set(was);
      handleErr(e);
    } finally {
      S.pending.delete(key);
    }
  }
  async function replan() {
    const btn = T && T.st.btn;
    if (!btn || btn.classList.contains('loading')) return;
    btn.classList.add('loading');
    btn.setAttribute('aria-busy', 'true');
    haptic('impact', 'medium');
    try {
      const d = await api('POST', 'api/replan');
      S.today = d;
      render(true);
      const r = d.replan || {};
      const n = r.placed || 0;
      toast('День пересобран: ' + n + ' ' + plural(n, 'квест', 'квеста', 'квестов') + ' на местах' +
        (r.unplaced ? ', без времени ' + r.unplaced : ''));
    } catch (e) {
      handleErr(e);
    } finally {
      if (btn.isConnected) { btn.classList.remove('loading'); btn.removeAttribute('aria-busy'); }
    }
  }

  // ---------- листы: меню квеста и новый квест ----------
  function openSheet(title, kids) {
    if (closeSheet) closeSheet();
    const prev = document.activeElement;
    const id = 'sheet-' + Date.now();
    const sheet = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id }, h('h2', { id: id }, title), kids);
    const bd = h('div', { class: 'backdrop' }, sheet);
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    const back = () => close();
    function close() {
      if (!bd.isConnected) return;
      bd.remove();
      document.removeEventListener('keydown', onKey);
      if (tg && tg.BackButton && tgv('6.1')) { try { tg.BackButton.offClick(back); tg.BackButton.hide(); } catch (e) { /* ок */ } }
      closeSheet = null;
      if (prev && prev.isConnected && prev.focus) prev.focus();
    }
    bd.addEventListener('click', (e) => { if (e.target === bd) close(); });
    document.addEventListener('keydown', onKey);
    if (tg && tg.BackButton && tgv('6.1')) { try { tg.BackButton.onClick(back); tg.BackButton.show(); } catch (e) { /* ок */ } }
    app.append(bd);
    closeSheet = close;
    const first = sheet.querySelector('input, button');
    if (first) setTimeout(() => first.focus(), 40);
    return close;
  }
  function taskMenu(t) {
    let close = null;
    const items = [];
    if (!t.done) {
      items.push(h('button', { type: 'button', class: 'btn-line', onclick: () => { close(); act(t, 'tomorrow'); } }, icon('move', 20), 'На завтра'));
    }
    items.push(h('button', { type: 'button', class: 'btn-line danger', onclick: () => { close(); confirmDrop(t); } }, icon('trash', 20), 'Удалить'));
    items.push(h('button', { type: 'button', class: 'btn-line quiet', onclick: () => close() }, 'Отмена'));
    close = openSheet(t.title, items);
  }
  function confirmDrop(t) {
    const text = 'Удалить квест «' + t.title + '»?';
    const go = (ok) => { if (ok) act(t, 'drop'); };
    if (tg && tgv('6.2') && tg.showConfirm) {
      try { tg.showConfirm(text, go); return; } catch (e) { /* ниже обычный confirm */ }
    }
    go(window.confirm(text));
  }
  async function act(t, action) {
    try {
      await api('POST', 'api/tasks/' + t.id + '/' + action);
      haptic('impact', 'light');
      toast(action === 'drop' ? 'Квест удалён' : 'Перенесено на завтра');
      S.list = { week: null, later: null };
      if (S.tab !== 'today') S.today = null;
      await refreshTab();
    } catch (e) { handleErr(e); }
  }
  function whenLabel(t) {
    const today = todayIso();
    if (t.due) {
      const d = t.due === today ? 'сегодня' : t.due === addDays(today, 1) ? 'завтра' : shortDate(t.due);
      return d + (t.time ? ' в ' + t.time : '');
    }
    return t.week_of ? 'на неделе' : 'потом';
  }
  function addForm(idp, bucket, onDone) {
    const input = h('input', { class: 'input', id: idp, type: 'text', placeholder: 'Новый квест', autocomplete: 'off',
      enterkeyhint: 'done', maxlength: 500, 'aria-describedby': idp + '-hint' });
    const btn = h('button', { type: 'submit', class: 'btn-gold' }, 'Добавить');
    const form = h('form', { class: 'addform' }, h('label', { class: 'sr', for: idp }, 'Текст квеста'), input, btn);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) { input.focus(); return; }
      btn.disabled = true;
      try {
        const t = await api('POST', 'api/tasks', { text: text, bucket: typeof bucket === 'function' ? bucket() : bucket });
        input.value = '';
        haptic('notify', 'success');
        if (onDone) onDone();
        toast('Квест записан · ' + whenLabel(t));
        S.list = { week: null, later: null };
        await refreshTab();
      } catch (err) {
        handleErr(err);
      } finally {
        btn.disabled = false;
      }
    });
    return [form, h('p', { class: 'hint', id: idp + '-hint' }, HINT)];
  }
  function openAdd(bucket) {
    let close = null;
    close = openSheet('Новый квест', addForm('add-text', bucket, () => close && close()));
  }

  // ---------- «Журнал» ----------
  function screenJournal() {
    const data = S.list[S.view];
    const seg = (v, label) => h('button', { type: 'button', 'aria-pressed': String(S.view === v), onclick: () => {
      if (S.view === v) return;
      S.view = v;
      haptic('select');
      render(true);
      refreshTab();
    } }, label);
    const list = h('div', { class: 'groups' });
    if (!data) list.append(h('div', { class: 'loading', role: 'status' }, 'Листаем журнал…'));
    else if (!data.groups.length) list.append(h('p', { class: 'empty' }, S.view === 'week' ? 'До конца недели квестов нет.' : 'Отложенных квестов нет.'));
    else {
      for (const g of data.groups) {
        list.append(h('section', { class: 'group' }, secTitle(g.title, null, String(g.items.length)), g.items.map((t) => taskCard(t, 'list'))));
      }
    }
    return h('div', { class: 'screen plain' },
      h('header', { class: 'head' }, h('span', { class: 'kicker' }, data && data.title ? data.title : 'Журнал'), h('h1', null, 'Журнал квестов')),
      h('div', { class: 'seg', role: 'group', 'aria-label': 'Что показать' }, seg('week', 'Неделя'), seg('later', 'Потом')),
      addForm('j-text', () => (S.view === 'week' ? 'week' : 'later')), list);
  }

  // ---------- «Персонаж» ----------
  function talkLines(c) {
    const out = [];
    if (c.open_rewards && c.open_rewards.length) out.push('Сундук открыт: ' + c.open_rewards[0].title + '. Забирай!');
    if (c.next_reward) {
      const need = Math.max(0, xpForLevel(c.next_reward.level) - c.xp);
      out.push('Ещё ' + fmt(need) + ' опыта, и сундук ' + c.next_reward.level + ' уровня наш.');
    } else {
      out.push('До ' + (c.level + 1) + ' уровня ' + fmt(c.next_level_at - c.xp) + ' опыта. Возьмём квест?');
    }
    const rusty = (c.attributes || []).filter((a) => a.rusty).sort((a, b) => (b.idle_days == null ? 1e9 : b.idle_days) - (a.idle_days == null ? 1e9 : a.idle_days))[0];
    if (rusty) out.push(rusty.idle_days != null ? '«' + rusty.title + '» без опыта ' + days(rusty.idle_days) + '. Займёмся?' : '«' + rusty.title + '» ещё ждёт первого опыта. Начнём?');
    const st = (c.streaks || [])[0];
    if (st && st.days > 1) out.push(st.title + ': ' + days(st.days) + ' подряд. Держим строй!');
    const strong = (c.attributes || []).slice().sort((a, b) => b.xp - a.xp)[0];
    if (strong && strong.xp > 0) out.push('Сильнее всего у нас «' + strong.title + '», уровень ' + strong.level + '.');
    if (c.main_quest) out.push('Помню главный квест: ' + c.main_quest);
    if (!out.length) out.push('Тихо у костра. Возьмём квест?');
    return out;
  }
  function poke(sc) {
    const c = S.character;
    const lines = talkLines(c);
    const line = lines[S.talk % lines.length];
    S.talk += 1;
    heroCheer(sc, 1600);
    haptic('impact', 'light');
    const old = sc.el.querySelector('.dialog');
    if (old) old.remove();
    const dlg = h('div', { class: 'dialog', role: 'status' }, h('span', { class: 'who', 'aria-hidden': 'true' }, 'Георгий'), h('span', { class: 'say' }, line));
    sc.el.append(dlg);
    clearTimeout(sc.dt);
    sc.dt = setTimeout(() => dlg.remove(), 3300);
  }
  function rewardLine(c) {
    if (c.open_rewards && c.open_rewards.length) {
      const r = c.open_rewards[0];
      return h('span', { class: 'reward open' }, 'Сундук ' + r.level + ' уровня открыт: ' + r.title);
    }
    if (c.next_reward) {
      const need = Math.max(0, xpForLevel(c.next_reward.level) - c.xp);
      return h('span', { class: 'reward' }, 'Сундук ' + c.next_reward.level + ' уровня: ' + c.next_reward.title + ' · ещё ' + fmt(need) + ' опыта');
    }
    return h('span', { class: 'reward' }, 'До ' + (c.level + 1) + ' уровня ещё ' + fmt(c.next_level_at - c.xp) + ' опыта');
  }
  function screenCharacter() {
    const c = S.character;
    let sc = null;
    sc = buildScene('big', { label: 'Георгий. Нажми, чтобы поговорить', onHero: () => poke(sc) });
    sc.el.append(h('div', { class: 'season kicker' }, c.season + ' · ' + seasonName()));
    const span = c.next_level_at - c.level_floor;
    const status = h('div', { class: 'panel status char' }, corners(),
      h('div', { class: 'lvl', 'aria-hidden': 'true' }, h('span', null, c.level)),
      h('div', { class: 'stack' },
        h('div', { class: 'line' }, h('h1', { class: 'name' }, 'Георгий'), h('span', { class: 'xptext' }, fmt(c.xp) + ' / ' + fmt(c.next_level_at))),
        h('div', { class: 'bar', role: 'progressbar', 'aria-label': 'Опыт до ' + (c.level + 1) + ' уровня', 'aria-valuemin': 0,
          'aria-valuemax': span, 'aria-valuenow': c.xp - c.level_floor }, h('i', { style: { width: pctOf(c) + '%' } })),
        rewardLine(c)));

    const body = h('div', { class: 'charbody' });
    body.append(h('div', { class: 'mainquest' + (c.main_quest ? '' : ' none') }, h('span', { class: 'mq-h' }, 'Главный квест'),
      h('span', { class: 'mq-t' }, c.main_quest || 'Появится, когда сетка придёт из «Мандарата».')));

    const attrs = c.attributes || [];
    const aSec = h('div', { class: 'group' }, secTitle('Характеристики', 'attr-title'));
    if (!attrs.length) aSec.append(h('p', { class: 'empty' }, 'Характеристик пока нет: их задаёт сетка целей.'));
    else {
      aSec.append(h('div', { class: 'attrs' }, attrs.map((a) => h('div', { class: 'attr' + (a.rusty ? ' rusty' : ''), role: 'img',
        'aria-label': a.title + ': уровень ' + a.level + ', ' + a.pct + '% до следующего' + (a.rusty ? ', ржавеет' : '') },
      h('div', { class: 'line' }, h('span', { class: 'an' }, a.title), h('span', { class: 'al' }, a.level)),
      h('div', { class: 'ab' }, h('i', { style: { width: Math.max(0, Math.min(100, a.pct)) + '%' } }))))));
      const rust = attrs.filter((a) => a.rusty).sort((a, b) => (b.idle_days == null ? 1e9 : b.idle_days) - (a.idle_days == null ? 1e9 : a.idle_days))[0];
      if (rust) {
        aSec.append(h('div', { class: 'rustnote' }, h('span', { class: 'rd', 'aria-hidden': 'true' }),
          rust.idle_days != null ? '«' + rust.title + '» ржавеет: ' + days(rust.idle_days) + ' без опыта. Любое дело из этой области снимет ржавчину.'
            : '«' + rust.title + '» ещё без опыта. Любое дело из этой области даст первый.'));
      }
    }
    body.append(aSec);

    const streaks = (c.streaks || []).filter((s) => s.days > 0).slice(0, 4);
    if (streaks.length) {
      body.append(h('div', { class: 'group' }, secTitle('Серии'),
        h('div', { class: 'chips' }, streaks.map((s) => h('span', { class: 'chip' }, s.title + ' · ', h('b', null, s.days), ' ' + plural(s.days, 'день', 'дня', 'дней'))))));
    }

    const ach = c.achievements || [];
    const kinds = ['flame', 'heart', 'star'];
    const badges = ach.map((a, i) => ({ name: a.title, aria: 'Трофей: ' + a.title + (a.detail ? ', ' + a.detail : ''), icon: kinds[i % 3] }));
    while (badges.length < 4) badges.push({ name: 'впереди', aria: 'Трофей ещё не получен', icon: 'lock', locked: true });
    body.append(h('div', { class: 'group' }, secTitle('Трофеи', null, String(ach.length)),
      h('div', { class: 'badges' }, badges.map((b) => {
        const ic = BADGES[b.icon];
        return h('div', { class: 'badge' + (b.locked ? ' locked' : ''), role: 'img', 'aria-label': b.aria },
          h('span', { class: 'ring', 'aria-hidden': 'true' }, h('span', { style: { position: 'absolute', left: '8px', top: '8px', width: '3px', height: '3px', boxShadow: spr(ic[0], ic[1], 3) } })),
          h('span', { class: 'bn', 'aria-hidden': 'true' }, b.name));
      }))));
    return h('div', { class: 'screen' }, sc.el, status, body);
  }

  // ---------- «Карта» ----------
  const KIND = { habit: ['привычка', '#8FE0BC'], milestone: ['этап', '#F5D58A'], quality: ['качество', '#C9A6F2'] };
  function screenMap() {
    const g = S.grid;
    const dirs = g.dirs || [];
    const opened = dirs.reduce((a, d) => a + d.filled, 0) + dirs.length + (g.center ? 1 : 0);
    const root = h('div', { class: 'screen plain' },
      h('header', { class: 'head' }, h('span', { class: 'kicker' }, (g.season || 'Сезон 1') + ' · открыто ' + opened + ' из 73'), h('h1', null, 'Карта целей')));
    const bodyEl = h('div', { class: 'mapbody' });
    root.append(bodyEl);
    if (!dirs.length && !g.center) {
      bodyEl.append(h('p', { class: 'empty' }, 'Карта ещё не нарисована: сетка целей появится после засева из «Мандарата».'));
      return root;
    }
    if (!S.sel || (S.sel !== 'C' && !dirs.some((d) => d.id === S.sel))) S.sel = dirs.length ? dirs[0].id : 'C';
    const pick = (id) => { if (S.sel !== id) { S.sel = id; haptic('select'); render(true); } };
    const gems = (n, all) => h('span', { class: 'gems', 'aria-hidden': 'true' }, Array.from({ length: 8 }, (_, i) => h('i', { class: all || i < n ? 'on' : '' })));
    const slots = [dirs[0], dirs[1], dirs[2], dirs[3], 'C', dirs[4], dirs[5], dirs[6], dirs[7]];
    const tiles = h('div', { class: 'tiles' }, slots.map((d) => {
      if (d === 'C') {
        const title = g.center ? g.center.title : 'Главный квест';
        return h('button', { type: 'button', class: 'tile center', 'aria-pressed': String(S.sel === 'C'), 'aria-label': 'Главный квест: ' + title, onclick: () => pick('C') },
          h('span', { class: 'tn' }, title), gems(8, true));
      }
      if (!d) return h('div', { class: 'tile void', 'aria-hidden': 'true' }, h('span', { class: 'tn' }, 'пусто'));
      return h('button', { type: 'button', class: 'tile' + (d.rusty ? ' rusty' : ''), 'aria-pressed': String(S.sel === d.id),
        'aria-label': d.title + ', уровень ' + d.level + ', открыто ' + d.filled + ' из 8' + (d.rusty ? ', ржавеет' : '') + ', открыть область', onclick: () => pick(d.id) },
      h('span', { class: 'line' }, h('span', { class: 'tn' }, d.title), h('span', { class: 'tl' }, d.level)), gems(d.filled));
    }));
    bodyEl.append(tiles);

    const cell = (c) => {
      if (!c) return h('div', { class: 'cell void' }, h('span', { class: 'cn' }, '+ пустая клетка'));
      const k = KIND[c.kind] || ['характеристика', '#E8C170'];
      return h('div', { class: 'cell' }, h('span', { class: 'cn' }, c.title),
        h('span', { class: 'col' }, h('span', { class: 'ck', style: { color: k[1] } }, h('i', { style: { background: k[1] }, 'aria-hidden': 'true' }), k[0]),
          c.stat ? h('span', { class: 'cs' }, c.stat) : null));
    };
    let heading, count, cells;
    if (S.sel === 'C') {
      heading = 'Главный квест';
      count = dirs.length + ' из 8';
      const around = dirs.map((d) => ({ title: d.title, kind: 'attribute', stat: 'ур. ' + d.level + (d.rusty ? ' · ржавеет' : '') }));
      cells = [0, 1, 2, 3].map((i) => cell(around[i])).concat([h('div', { class: 'cell mid quest' }, h('span', { class: 'cn' }, g.center ? g.center.title : 'Главный квест'))],
        [4, 5, 6, 7].map((i) => cell(around[i])));
    } else {
      const d = dirs.find((x) => x.id === S.sel);
      heading = d.title + ' · ур. ' + d.level;
      count = d.filled + ' из 8';
      cells = [0, 1, 2, 3].map((i) => cell(d.cells[i])).concat([h('div', { class: 'cell mid' }, h('span', { class: 'cn' }, d.title))],
        [4, 5, 6, 7].map((i) => cell(d.cells[i])));
    }
    bodyEl.append(secTitle(heading, 'map-detail', count), h('div', { class: 'cells', role: 'group', 'aria-labelledby': 'map-detail' }, cells),
      h('div', { class: 'legend' }, Object.keys(KIND).map((k) => h('span', null, h('i', { style: { background: KIND[k][1] }, 'aria-hidden': 'true' }), KIND[k][0]))));
    return root;
  }

  // ---------- заглушки ----------
  function gate(title, text, retry) {
    return h('div', { class: 'screen' }, h('div', { class: 'gate' }, h('h1', null, title), h('p', null, text),
      retry ? h('button', { type: 'button', class: 'btn-gold', onclick: () => show(S.tab) }, 'Повторить') : null,
      !INIT && !DEMO && !DEV ? h('a', { class: 'btn-gold', href: '?demo=1' }, 'Посмотреть демо') : null));
  }

  // ---------- демо: те же формы данных, состояние в памяти ----------
  const Demo = (function () {
    const CTX = { infatica: ['Инфатика', '#4FB6C2'], alfa: ['Альфа', '#F08FB0'], consulting: ['Консалтинг', '#E3C26B'], personal: ['Личное', '#A3B0FF'] };
    const XP = { S: 10, M: 25, L: 60 };
    const MIN = { S: 30, M: 60, L: 120 };
    const base = new Date(Date.now() - 4 * 3600e3);
    const day0 = new Date(base.getFullYear(), base.getMonth(), base.getDate());
    const T0 = isoD(day0);
    const at = (hh, mm, plus) => { const d = new Date(day0); d.setDate(d.getDate() + (plus || 0)); d.setHours(hh, mm || 0, 0, 0); return d; };
    const dayIso = (plus) => isoD(at(0, 0, plus));
    const hm = (d) => pad(d.getHours()) + ':' + pad(d.getMinutes());
    const monday = dayIso(-wdIdx(day0));
    const sunday = addDays(monday, 6);
    const lvlOf = (xp) => Math.floor((Math.sqrt(1 + 0.08 * xp) - 1) / 2);
    const st = { xp: 3580, nextId: 100 };
    let tasks = [
      { id: 1, title: 'Ответить Саше Петрову про SDK', ctx: 'infatica', size: 'S', start: at(9, 30), due: T0, done: true, doneDay: T0, got: 10, cell: 'Карьера' },
      { id: 2, title: 'Отчёт по white-label SDK', ctx: 'infatica', size: 'M', start: at(11, 0), due: T0, cell: 'Карьера' },
      { id: 3, title: 'Черновик поста для Unrote', ctx: 'consulting', size: 'L', start: at(15, 0), due: T0, cell: 'Консалтинг' },
      { id: 4, title: 'Позвонить в банк', ctx: 'personal', size: 'S', start: at(17, 30), due: T0, cell: 'Деньги' },
      { id: 5, title: 'Продлить страховку мотоцикла', ctx: 'personal', size: 'S', due: dayIso(-2), cell: 'Дом' },
      { id: 6, title: 'Ревью дорожной карты SDK', ctx: 'infatica', size: 'M', due: dayIso(1), cell: 'Карьера' },
      { id: 7, title: 'Отправить счёт клиенту Unrote', ctx: 'consulting', size: 'S', due: dayIso(2), cell: 'Деньги' },
      { id: 8, title: 'Собрать кейс для консалтинга', ctx: 'consulting', size: 'L', week_of: monday, cell: 'Консалтинг' },
      { id: 9, title: 'Подготовить вопросы к ретро Альфы', ctx: 'alfa', size: 'S', week_of: monday, cell: 'Карьера' },
      { id: 10, title: 'Книга про продуктовые метрики', ctx: 'personal', size: 'M', cell: 'Знания' },
      { id: 11, title: 'Разобрать архив фотографий', ctx: 'personal', size: 'L', due: dayIso(9), cell: 'Дом' },
    ];
    const meetings = [
      { title: 'Daily Infatica', ctx: 'infatica', s: at(10, 0), e: at(10, 30) },
      { title: 'Альфа: синк по поддержке', ctx: 'alfa', s: at(13, 0), e: at(14, 0) },
    ];
    const habits = [
      { cell_id: 'A1', title: 'Сон 7,5 ч', note: 'из Garmin', done: true, auto: true, via: 'garmin' },
      { cell_id: 'A2', title: 'Растяжка', note: 'серия 4', done: false, auto: false, via: null },
      { cell_id: 'B3', title: 'Оценка дня', note: 'оценкой дня', done: false, auto: true, via: 'rating' },
      { cell_id: 'E1', title: 'Английский', note: '+10 опыта', done: false, auto: false, via: null },
    ];
    const rated = { score: null, past: [3, 5, -2, 4, 6, 2, 0, 7, 4, 3, -1, 5] };      // прошлые дни месяца
    function rating() {
      const all = rated.past.concat(rated.score == null ? [] : [rated.score]);
      const avg = Math.round(all.reduce((a, b) => a + b, 0) * 10 / all.length) / 10;
      return { day: T0, score: rated.score, note: null, month_avg: avg, month_days: all.length };
    }
    function rate(body) {
      const v = body && body.score;
      if (!Number.isInteger(v) || v < -10 || v > 10) throw Object.assign(new Error('score: целое от -10 до +10'), { status: 400 });
      const first = rated.score == null;
      const before = lvlOf(st.xp);
      rated.score = v;
      const hb = habits.find((x) => x.via === 'rating');
      const xp = first ? 15 : 0;
      if (first && hb) hb.done = true;
      st.xp += xp;
      return { rating: rating(), xp: xp, milestone: null, week_avg: null, level_up: lvlOf(st.xp) > before, character: character() };
    }
    function character() {
      const L = lvlOf(st.xp);
      return { name: 'Георгий', level: L, xp: st.xp, level_floor: 50 * L * (L + 1), next_level_at: 50 * (L + 1) * (L + 2) };
    }
    function out(t) {
      const c = CTX[t.ctx] || ['', '#A9B0D6'];
      const end = t.start ? new Date(t.start.getTime() + MIN[t.size] * 60000) : null;
      return { id: t.id, title: t.title, context_id: t.ctx || null, context_title: c[0], color: c[1], size: t.size, xp: XP[t.size],
        cell_id: t.cell ? 'X' : null, cell_title: t.cell || null, due: t.due || null, week_of: t.week_of || null,
        time: t.start ? hm(t.start) : null, start: t.start ? t.start.toISOString() : null, end: end ? end.toISOString() : null,
        done: !!t.done, overdue: !t.done && !!t.due && t.due < T0, remind_at: null };
    }
    function isoWeek(d) {
      const x = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
      const dn = x.getUTCDay() || 7;
      x.setUTCDate(x.getUTCDate() + 4 - dn);
      const y0 = new Date(Date.UTC(x.getUTCFullYear(), 0, 1));
      return Math.ceil(((x - y0) / 864e5 + 1) / 7);
    }
    function today() {
      const now = new Date();
      const items = [], uns = [];
      const list = tasks.filter((t) => !t.dropped && ((!t.done && t.due && t.due <= T0) || (t.done && t.doneDay === T0)));
      for (const t of list) {
        const o = out(t);
        if (t.start && t.due === T0) items.push([t.start, new Date(o.end), 2, Object.assign({ type: 'task' }, o)]);
        else uns.push(o);
      }
      uns.sort((a, b) => (a.done - b.done) || (b.overdue - a.overdue) || (a.id - b.id));
      for (const m of meetings) {
        const c = CTX[m.ctx];
        items.push([m.s, m.e, 1, { type: 'meeting', title: m.title, context_id: m.ctx, color: c[1], time: hm(m.s), start: m.s.toISOString(),
          end: m.e.toISOString(), meta: hm(m.s) + '–' + hm(m.e) + ' · ' + c[0] }]);
      }
      const ts = at(21, 30), te = at(23, 0);
      items.push([ts, te, 3, { type: 'training', time: '21:30', title: 'Тренировка', meta: '21:30–23:00 · Здоровье', start: ts.toISOString(), end: te.toISOString(), done: false }]);
      const sorted = items.slice().sort((a, b) => a[0] - b[0]);
      let cur = new Date(Math.max(at(8, 0).getTime(), Math.ceil(now.getTime() / 300000) * 300000));
      const wake = at(1, 0, 1);
      for (const it of sorted) {
        const end = new Date(Math.min(it[0].getTime(), wake.getTime()));
        const mins = Math.round((end - cur) / 60000);
        if (mins >= 45) {
          const hh = Math.floor(mins / 60), mm = mins % 60;
          const dur = hh && mm ? hh + ' ч ' + mm + ' мин' : hh ? hh + ' ч' : mm + ' мин';
          items.push([cur, end, 0, { type: 'gap', time: hm(cur), title: 'Привал · свободно ' + dur, minutes: mins }]);
        }
        if (it[1] > cur) cur = it[1];
      }
      items.push([now, now, -1, { type: 'now', time: hm(now) }]);
      items.sort((a, b) => (a[0] - b[0]) || (a[2] - b[2]));
      const done = list.filter((t) => t.done).length;
      const xpToday = list.filter((t) => t.done).reduce((a, t) => a + (t.got || 0), 0) + habits.filter((x) => x.done && !x.auto).length * 10
        + (rated.score != null ? 15 : 0);
      return { date: T0, date_label: WD_FULL[wdIdx(day0)].replace(/^./, (s) => s.toUpperCase()) + ', ' + dayMonth(T0), week: isoWeek(day0),
        now: now.toISOString(), character: character(), summary: { done: done, total: list.length, xp_today: xpToday },
        habits: habits.map((x) => Object.assign({}, x)), rating: rating(), timeline: items.map((i) => i[3]), unscheduled: uns };
    }
    function listView(v) {
      const open = tasks.filter((t) => !t.done && !t.dropped);
      const groups = [];
      const byDate = (ts) => {
        const m = new Map();
        ts.sort((a, b) => (a.due < b.due ? -1 : a.due > b.due ? 1 : a.id - b.id)).forEach((t) => {
          if (!m.has(t.due)) m.set(t.due, { title: shortDate(t.due).replace(/^./, (s) => s.toUpperCase()), date: t.due, items: [] });
          m.get(t.due).items.push(out(t));
        });
        return Array.from(m.values());
      };
      if (v === 'week') {
        groups.push.apply(groups, byDate(open.filter((t) => t.due && t.due > T0 && t.due <= sunday)));
        const wk = open.filter((t) => !t.due && t.week_of && t.week_of <= monday);
        if (wk.length) groups.push({ title: 'На неделе, без дня', date: null, items: wk.map(out) });
      } else {
        groups.push.apply(groups, byDate(open.filter((t) => t.due && t.due > sunday)));
        const none = open.filter((t) => !t.due && (!t.week_of || t.week_of > monday));
        if (none.length) groups.push({ title: 'Без даты', date: null, items: none.map(out) });
      }
      return { view: v, title: 'Неделя ' + isoWeek(day0) + ' · ' + dayMonth(monday) + '–' + dayMonth(sunday), groups: groups };
    }
    function create(body) {
      let text = String((body && body.text) || '').trim();
      if (!text) throw Object.assign(new Error('пустой текст'), { status: 400 });
      const t = { id: ++st.nextId, ctx: 'personal', size: 'S', cell: null };
      const tm = text.match(/(?:^|\s)в\s+(\d{1,2})(?::(\d{2}))?(?=\s|$)/);
      if (/(?:^|\s)завтра(?=\s|$)/.test(text)) { t.due = dayIso(1); text = text.replace(/(?:^|\s)завтра(?=\s|$)/, ' '); }
      if (tm) { text = text.replace(tm[0], ' '); }
      if (!t.due) {
        if (body.bucket === 'today') t.due = T0;
        else if (body.bucket === 'week') t.week_of = monday;
      }
      if (tm && t.due) t.start = at(+tm[1], +(tm[2] || 0), t.due === T0 ? 0 : 1);
      t.title = text.replace(/\s+/g, ' ').trim() || 'Новый квест';
      tasks.push(t);
      return out(t);
    }
    function taskAct(id, action) {
      const t = tasks.find((x) => x.id === id && !x.dropped);
      if (!t) throw Object.assign(new Error('not_found'), { status: 404 });
      const before = lvlOf(st.xp);
      if (action === 'done') {
        let got = 0;
        if (!t.done) { t.done = true; t.doneDay = T0; got = t.cell ? XP[t.size] : 0; t.got = got; st.xp += got; }
        return { task: out(t), xp: got, level_up: lvlOf(st.xp) > before, character: character() };
      }
      if (action === 'undone') {
        if (t.done) { st.xp -= t.got || 0; t.got = 0; t.done = false; t.doneDay = null; }
        return { task: out(t), character: character() };
      }
      if (action === 'tomorrow') { t.due = dayIso(1); t.week_of = null; t.start = null; return out(t); }
      if (action === 'drop') { t.dropped = true; return { ok: true }; }
      throw Object.assign(new Error('not_found'), { status: 404 });
    }
    function habit(id) {
      const x = habits.find((y) => y.cell_id === id);
      if (!x) throw Object.assign(new Error('not_found'), { status: 404 });
      if (x.auto) throw Object.assign(new Error('эта привычка отмечается сама'), { status: 400 });
      const before = lvlOf(st.xp);
      x.done = !x.done;
      st.xp += x.done ? 10 : -10;
      return { done: x.done, xp: x.done ? 10 : 0, level_up: lvlOf(st.xp) > before, character: character() };
    }
    function characterView() {
      const ch = character();
      const A = [['A', 'Здоровье', 6, 62, false, 2], ['B', 'Карьера', 8, 30, false, 0], ['C', 'Деньги', 4, 75, false, 1], ['D', 'Близкие', 3, 10, true, 16],
        ['E', 'Знания', 5, 48, false, 3], ['F', 'Консалтинг', 7, 85, false, 0], ['G', 'Дом', 2, 20, false, 6], ['H', 'Свобода', 4, 55, false, 4]];
      return Object.assign(ch, {
        season: 'Сезон 1', main_quest: 'Пример главного квеста: одна проверяемая фраза на 2-3 года',
        attributes: A.map((a) => ({ id: a[0], title: a[1], level: a[2], xp: 50 * a[2] * (a[2] + 1) + a[3], pct: a[3], rusty: a[4], idle_days: a[5] })),
        next_reward: { level: ch.level + 1, title: 'вещь из списка хотелок' }, open_rewards: [],
        achievements: [{ code: 'sleep12', title: 'Сон ×12', detail: 'сон по норме 12 дней подряд' }, { code: 'med', title: 'Медосмотр', detail: 'пройден' },
          { code: 'week5', title: 'Неделя 5/5', detail: 'неделя без пропусков' }],
        streaks: [{ cell_id: 'A1', title: 'Сон 7,5 ч', days: 12 }, { cell_id: 'A2', title: 'Растяжка', days: 4 }, { cell_id: 'E1', title: 'Английский', days: 0 }],
      });
    }
    const c = (id, title, kind, stat) => ({ id: id, title: title, kind: kind, stat: stat, auto: false });
    const GRID = {
      season: 'Сезон 1', center: { id: 'C', title: 'Пример главного квеста' },
      dirs: [
        { id: 'A', title: 'Здоровье', level: 6, filled: 7, rusty: false, cells: [c('A1', 'Сон 7,5 ч', 'habit', 'серия 12'), c('A2', 'Тренировка 3 раза в неделю', 'habit', 'серия 4'),
          c('A3', 'Растяжка', 'habit', 'серия 0'), c('A4', '10 км за 55 минут', 'milestone', 'босс'), c('A5', 'Медосмотр', 'milestone', 'сделано'),
          c('A6', 'Спокойствие', 'quality', '3 из 4 недель'), c('A7', 'Вода 2 л', 'habit', 'серия 7')] },
        { id: 'B', title: 'Карьера', level: 8, filled: 6, rusty: false, cells: [c('B1', 'Отчёт по спринту', 'habit', 'серия 2'), c('B2', 'Оффер мечты', 'milestone', 'впереди')] },
        { id: 'C', title: 'Деньги', level: 4, filled: 5, rusty: false, cells: [c('C1', 'Учёт расходов', 'habit', 'серия 9')] },
        { id: 'D', title: 'Близкие', level: 3, filled: 4, rusty: true, cells: [c('D1', 'Звонок родителям', 'habit', 'серия 0')] },
        { id: 'E', title: 'Знания', level: 5, filled: 6, rusty: false, cells: [c('E1', 'Английский', 'habit', 'серия 0')] },
        { id: 'F', title: 'Консалтинг', level: 7, filled: 5, rusty: false, cells: [c('F1', 'Пост в неделю', 'quality', '2 из 4 недель')] },
        { id: 'G', title: 'Дом', level: 2, filled: 3, rusty: false, cells: [c('G1', 'Порядок по субботам', 'habit', 'серия 1')] },
        { id: 'H', title: 'Свобода', level: 4, filled: 4, rusty: false, cells: [c('H1', 'Виза номада', 'milestone', 'впереди')] },
      ],
    };
    async function handle(method, path, body) {
      await new Promise((r) => setTimeout(r, 120));
      const parts = path.split('?');
      const p = parts[0].replace(/^\/?api\//, '');
      const q = new URLSearchParams(parts[1] || '');
      if (p === 'today') return today();
      if (p === 'list') return listView(q.get('view') === 'later' ? 'later' : 'week');
      if (p === 'character') return characterView();
      if (p === 'grid') return GRID;
      if (p === 'tasks') return create(body || {});
      if (p === 'rating') return rate(body);
      if (p === 'replan') {
        const open = tasks.filter((t) => !t.done && !t.dropped && t.due === T0);
        return Object.assign(today(), { replan: { placed: open.filter((t) => t.start).length, unplaced: open.filter((t) => !t.start).length } });
      }
      let m = p.match(/^tasks\/(\d+)\/(done|undone|tomorrow|drop)$/);
      if (m) return taskAct(+m[1], m[2]);
      m = p.match(/^habits\/([^/]+)\/toggle$/);
      if (m) return habit(decodeURIComponent(m[1]));
      throw Object.assign(new Error('not_found'), { status: 404 });
    }
    return { handle: handle };
  })();

  // ---------- запуск ----------
  function initTelegram() {
    if (!tg) return;
    try { tg.ready(); } catch (e) { /* ок */ }
    try { tg.expand(); } catch (e) { /* ок */ }
    const mobile = /^(ios|android)/.test(tg.platform || '');
    if (mobile && tgv('8.0') && typeof tg.requestFullscreen === 'function') { try { tg.requestFullscreen(); } catch (e) { /* ок */ } }
    if (tgv('7.7') && typeof tg.disableVerticalSwipes === 'function') { try { tg.disableVerticalSwipes(); } catch (e) { /* ок */ } }
    if (tgv('6.1')) {
      try { tg.setHeaderColor('#060A1D'); tg.setBackgroundColor('#0A0D22'); } catch (e) { /* ок */ }
    }
    if (tgv('7.10') && typeof tg.setBottomBarColor === 'function') { try { tg.setBottomBarColor('#0A0D22'); } catch (e) { /* ок */ } }
    try { tg.onEvent('activated', () => refreshTab()); } catch (e) { /* ок */ }
  }
  function start() {
    initTelegram();
    tabbar.replaceChildren.apply(tabbar, TABS.map((t) => h('button', { type: 'button', class: 'tab', 'data-tab': t[0], onclick: () => {
      if (S.tab !== t[0]) { haptic('select'); show(t[0]); } else view.scrollTo({ top: 0, behavior: reduced() ? 'auto' : 'smooth' });
    } }, icon(t[2], 22), t[1])));
    app.append(fab);
    if (!INIT && !DEV && !DEMO) {
      view.replaceChildren(gate('Квесты у костра', 'Открой приложение из бота в Telegram: кнопка меню рядом с полем ввода. Посмотреть, как всё устроено, можно в демо.', false));
      return;
    }
    if (DEMO) app.append(h('span', { class: 'demo-flag', 'aria-hidden': 'true' }, 'ДЕМО'));
    tabbar.hidden = false;
    show('today');
    document.addEventListener('visibilitychange', () => { if (!document.hidden && !closeSheet) refreshTab(); });
    setInterval(() => {
      if (!document.hidden && S.tab === 'today' && !closeSheet && Date.now() > (S.fxUntil || 0)) refreshTab();
    }, 5 * 60 * 1000);
  }
  start();
})();
