/* ============================================================
   phantom.js — Shared JS for Phantom storefront
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
  const toggle = document.querySelector('.nav-toggle');
  const mobile = document.querySelector('.nav-mobile');
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
    mobile.querySelectorAll('a').forEach(a => {
      a.addEventListener('click', () => {
        mobile.classList.remove('open');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.querySelectorAll('span').forEach(s => { s.style.transform = ''; s.style.opacity = ''; });
      });
    });
  }

  /* ── Product preview tabs (index page) ─────────────────── */
  const thumbs = document.querySelectorAll('.preview-thumb');
  const panels = document.querySelectorAll('.preview-panel');
  if (thumbs.length) {
    thumbs.forEach(tab => {
      tab.addEventListener('click', () => {
        const target = tab.dataset.tab;
        thumbs.forEach(t => { t.classList.remove('active'); t.setAttribute('aria-selected', 'false'); });
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
      const item   = btn.closest('.faq-item');
      const isOpen = item.classList.contains('open');
      document.querySelectorAll('.faq-item.open').forEach(el => {
        el.classList.remove('open');
        el.querySelector('.faq-question').setAttribute('aria-expanded', 'false');
      });
      if (!isOpen) {
        item.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
      }
    });
  });

  /* ── Access card hover effect (index pricing section) ──── */
  document.querySelectorAll('.access-card').forEach(card => {
    card.addEventListener('click', () => {
      const href = card.querySelector('a')?.getAttribute('href');
      if (href) window.location.href = href;
    });
  });

  /* ── Scroll-triggered fade-up ───────────────────────────── */
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver(
      (entries) => entries.forEach(e => {
        if (e.isIntersecting) {
          e.target.classList.add('in-view');
          io.unobserve(e.target);
        }
      }),
      { threshold: 0.1 }
    );
    document.querySelectorAll('.feature-card, .review-card, .stat-block, .access-card').forEach(el => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(18px)';
      el.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
      io.observe(el);
    });
    document.addEventListener('scroll', () => {}, { passive: true });
  }

  /* in-view trigger */
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.in-view').forEach(el => {
      el.style.opacity = '1';
      el.style.transform = 'none';
    });
  });

  /* ── Active nav link ────────────────────────────────────── */
  const path = window.location.pathname.replace(/\/$/, '') || '/';
  document.querySelectorAll('.nav-links a, .nav-mobile a').forEach(a => {
    const href = (a.getAttribute('href') || '').replace(/\/$/, '') || '/';
    if (
      href === path ||
      (path.endsWith('index.html') && (href === '/' || href === 'index.html')) ||
      href === window.location.hash
    ) {
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
      window.scrollTo({
        top: target.getBoundingClientRect().top + window.scrollY - offset,
        behavior: 'smooth'
      });
    });
  });

  /* ── IntersectionObserver CSS class trigger ─────────────── */
  setTimeout(() => {
    document.querySelectorAll('[style*="opacity: 0"]').forEach(el => {
      const observer = new IntersectionObserver(entries => {
        entries.forEach(entry => {
          if (entry.isIntersecting) {
            entry.target.style.opacity = '1';
            entry.target.style.transform = 'none';
            observer.unobserve(entry.target);
          }
        });
      }, { threshold: 0.08 });
      observer.observe(el);
    });
  }, 100);

})();
