document.documentElement.classList.add('motion-ready');

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

function startIntroMotion() {
  const root = document.documentElement;
  const hero = document.querySelector('.hero');
  const words = document.querySelectorAll('[data-reveal-word]');
  const animatedBlocks = document.querySelectorAll('.hero-visual, .hero-message');
  const sections = document.querySelectorAll('[data-section-reveal]');
  const parallaxStage = document.querySelector('[data-parallax-stage]');

  words.forEach((word, index) => word.style.setProperty('--word-index', index));

  if (reducedMotion.matches) {
    root.classList.add('motion-reduced', 'motion-active', 'motion-loop');
    return;
  }

  requestAnimationFrame(() => root.classList.add('motion-active'));
  window.setTimeout(() => root.classList.add('motion-loop'), 1280);

  if ('IntersectionObserver' in window && hero) {
    const observer = new IntersectionObserver(([entry]) => {
      root.classList.toggle('motion-paused', !entry.isIntersecting);
    }, { threshold: 0.05 });
    observer.observe(hero);
  }

  if ('IntersectionObserver' in window) {
    const sectionObserver = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('section-visible');
        sectionObserver.unobserve(entry.target);
      });
    }, { threshold: 0.12 });
    sections.forEach((section) => sectionObserver.observe(section));
  } else {
    sections.forEach((section) => section.classList.add('section-visible'));
  }

  if (parallaxStage) {
    let frame = 0;
    const updateStage = () => {
      frame = 0;
      const rect = parallaxStage.getBoundingClientRect();
      const viewport = window.innerHeight || 1;
      if (rect.bottom < 0 || rect.top > viewport) return;
      const progress = Math.max(-1, Math.min(1, (viewport / 2 - (rect.top + rect.height / 2)) / viewport));
      parallaxStage.style.setProperty('--stage-shift-small', `${progress * -18}px`);
      parallaxStage.style.setProperty('--stage-shift-large', `${progress * 26}px`);
    };
    const requestStageUpdate = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(updateStage);
    };
    updateStage();
    window.addEventListener('scroll', requestStageUpdate, { passive: true });
    window.addEventListener('resize', requestStageUpdate, { passive: true });
  }

  document.addEventListener('visibilitychange', () => {
    root.classList.toggle('motion-hidden', document.hidden);
  });

  animatedBlocks.forEach((block) => block.addEventListener('animationend', () => {
    block.classList.add('intro-complete');
  }, { once: true }));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startIntroMotion, { once: true });
} else {
  startIntroMotion();
}
