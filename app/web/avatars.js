// Only user-supplied portraits are bundled; missing roles keep a text fallback.
(() => {
  const sources = {
    planner: '',
    implementer: '',
    reviewer: '/ui/assets/avatars/reviewer.png',
  };
  const fallbackGlyphs = {planner: '白', implementer: '月', reviewer: '鲸'};

  function create(role, name) {
    const knownRole = Object.hasOwn(fallbackGlyphs, role);
    const avatar = document.createElement('span');
    avatar.className = `avatar ${knownRole ? role : 'system'}`;
    avatar.setAttribute('aria-hidden', 'true');

    const fallback = document.createElement('span');
    fallback.className = 'avatar-fallback';
    fallback.textContent = fallbackGlyphs[role] || (name || '?').slice(0, 1);
    avatar.append(fallback);

    const source = knownRole ? sources[role] : '';
    if (source) {
      const image = document.createElement('img');
      image.className = 'avatar-image';
      image.alt = '';
      image.decoding = 'async';
      image.hidden = true;
      image.addEventListener('load', () => {
        image.hidden = false;
        fallback.hidden = true;
      });
      image.addEventListener('error', () => {
        image.hidden = true;
        fallback.hidden = false;
      });
      image.src = source;
      avatar.append(image);
    }
    return avatar;
  }

  window.CodeCrewAvatars = {create, sources};
})();
