/* ============================================================
   ghost.js — Shared JS for Ghost storefront
   ============================================================ */

(function () {
  'use strict';

  /* ── Nav scroll shadow ──────────────────────────────────── */
  const nav = document.querySelector('.nav');
  if (nav) {
    const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 12);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
  }

  /* ── Mobile hamburger ───────────────────────────────────── */
  const toggle  = document.querySelector('.nav-toggle');
  const mobile  = document.querySelector('.nav-mobile');
  if (toggle && mobile) {
    toggle.addEventListener('click', () => {
      const open = mobile.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(open));
      toggle.querySelectorAll('span').forEach((s, i) => {
        if (open) {
          if (i === 0) s.style.transform = 'rotate(45deg) translate(5px, 5px)';
          if (i === 1) s.style.opacity   = '0';
          if (i === 2) s.style.transform = 'rotate(-45deg) translate(5px, -5px)';
        } else {
          s.style.transform = '';
          s.style.opacity   = '';
        }
      });
    });
    /* close on link click */
    mobile.querySelectorAll('a').forEach(a => {
      a.addEventListener('click', () => {
        mobile.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.querySelectorAll('span').forEach(s => { s.style.transform = ''; s.style.opacity = ''; });
      });
    });
  }

  /* ── Product preview tabs ───────────────────────────────── */
  const tabs = document.querySelectorAll('.preview-tab');
  const panels = document.querySelectorAll('.preview-panel');
  if (tabs.length) {
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.tab;
        tabs.forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
        panels.forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        tab.setAttribute('aria-selected', 'true');
        const panel = document.querySelector(`.preview-panel[data-panel="${target}"]`);
        if (panel) panel.classList.add('active');
      });
    });
  }

  /* ── FAQ accordion ──────────────────────────────────────── */
  document.querySelectorAll('.faq-question').forEach(btn => {
    btn.addEventListener('click', () => {
      const item = btn.closest('.faq-item');
      const isOpen = item.classList.contains('open');
      /* close all */
      document.querySelectorAll('.faq-item.open').forEach(el => el.classList.remove('open'));
      /* toggle clicked */
      if (!isOpen) item.classList.add('open');
    });
  });

  /* ── Scroll-triggered fade-up for cards ─────────────────── */
  const io = new IntersectionObserver(
    (entries) => entries.forEach(e => {
      if (e.isIntersecting) {
        e.target.classList.add('animate-up');
        io.unobserve(e.target);
      }
    }),
    { threshold: 0.12 }
  );
  document.querySelectorAll('.feature-card, .step, .pricing-card, .faq-item, .preview-panel-inner, .feat-illustration, .feature-row-copy').forEach(el => {
    el.style.opacity = '0';
    io.observe(el);
  });

  /* ── Active nav link (hash or pathname) ─────────────────── */
  const path = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.nav-links a, .nav-mobile a').forEach(a => {
    const href = (a.getAttribute('href') || '').replace(/\/$/, '') || '/';
    if (href === path || (path.endsWith('index.html') && href === '/') || href === window.location.hash) {
      a.classList.add('active');
    }
  });

  /* ── Smooth hash scroll with nav offset ─────────────────── */
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
      const target = document.querySelector(a.getAttribute('href'));
      if (!target) return;
      e.preventDefault();
      const offset = (nav ? nav.offsetHeight : 0) + 16;
      window.scrollTo({ top: target.getBoundingClientRect().top + window.scrollY - offset, behavior: 'smooth' });
    });
  });

})();
