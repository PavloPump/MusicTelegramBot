'use strict';

/* ═══════════════════════════════════════════════
   ZVONKO  –  app.js
═══════════════════════════════════════════════ */

const API = '';
const BOT = 'ZvonkoMusicbot';

// Proxy YouTube images to avoid connection issues
function proxyImage(url) {
  if (!url) return '';
  if (url.includes('yt3.googleusercontent.com') || url.includes('lh3.googleusercontent.com')) {
    return `${API}/api/proxy/image?url=${encodeURIComponent(url)}`;
  }
  return url;
}

// Add image error handler globally
document.addEventListener('DOMContentLoaded', () => {
  document.addEventListener('error', (e) => {
    if (e.target.tagName === 'IMG') {
      const img = e.target;
      const src = img.src;
      
      // Track retry attempts
      if (!img.dataset.retryCount) {
        img.dataset.retryCount = '0';
      }
      const retryCount = parseInt(img.dataset.retryCount);
      
      // Try alternative YouTube image sizes
      if (src.includes('yt3.googleusercontent.com') && retryCount < 3) {
        img.dataset.retryCount = (retryCount + 1).toString();
        
        // Try progressively smaller sizes
        const sizes = ['w120-h120-l90-rj', 'w60-h60-l90-rj', 'w30-h30-l90-rj'];
        const currentMatch = src.match(/w(\d+)-h(\d+)-l90-rj/);
        
        if (currentMatch) {
          const currentSize = `w${currentMatch[1]}-h${currentMatch[2]}-l90-rj`;
          const currentIndex = sizes.indexOf(currentSize);
          
          if (currentIndex < sizes.length - 1) {
            const newSrc = src.replace(/w\d+-h\d+-l90-rj/, sizes[currentIndex + 1]);
            img.src = newSrc;
            return;
          }
        }
      }
      
      // If all retries failed, show fallback icon
      img.onerror = null;
      img.style.display = 'none';
      const parent = img.parentElement;
      if (parent && !parent.querySelector('.fallback-icon')) {
        const icon = document.createElement('i');
        icon.className = 'fas fa-music fallback-icon';
        icon.style.cssText = 'font-size:32px;color:var(--text3);display:flex;align-items:center;justify-content:center;height:100%';
        parent.appendChild(icon);
      }
    }
  }, true);
});

/* ─── State ─────────────────────────────────── */
let currentUser   = null;
let playlist      = [];
let playIdx       = -1;
let isPlaying     = false;
let currentId     = null;
let previousPage  = 'home';
let searchData    = { tracks: [], artists: [] };
let activeTab     = 'tracks';
let isMobile      = window.innerWidth <= 768;
// Stream cache removed - always fetch fresh URLs
let preloadedNext = null; // Preloaded next track

/* ─── Audio ─────────────────────────────────── */
const audio = document.getElementById('audio');
audio.volume = 0.7;
audio.crossOrigin = 'anonymous'; // Enable CORS for audio

/* ─── Touch Gestures for iOS ─────────────────── */
let touchStartX = 0;
let touchStartY = 0;
let touchEndX = 0;
let touchEndY = 0;
let lastTap = 0;

function handleGesture() {
  const deltaX = touchEndX - touchStartX;
  const deltaY = touchEndY - touchStartY;
  const minSwipeDistance = 50;
  
  // Horizontal swipe (must be more horizontal than vertical)
  if (Math.abs(deltaX) > Math.abs(deltaY) && Math.abs(deltaX) > minSwipeDistance) {
    if (deltaX > 0) {
      // Swipe right - previous track
      showSwipeIndicator('left');
      playPrev();
    } else {
      // Swipe left - next track
      showSwipeIndicator('right');
      playNext();
    }
  }
}

function showSwipeIndicator(direction) {
  const fsPlayer = document.getElementById('fullscreen-player');
  if (!fsPlayer) return;
  
  const indicator = document.createElement('div');
  indicator.className = 'swipe-indicator';
  indicator.innerHTML = `<i class="fas fa-${direction === 'left' ? 'backward' : 'forward'}"></i>`;
  indicator.style.cssText = `
    position: absolute;
    top: 50%;
    ${direction}: 20%;
    transform: translateY(-50%);
    font-size: 48px;
    color: var(--accent);
    opacity: 0;
    animation: swipeIndicatorAnim 0.5s ease-out;
    pointer-events: none;
    z-index: 1000;
  `;
  
  fsPlayer.appendChild(indicator);
  setTimeout(() => indicator.remove(), 500);
}

// Add touch listeners to fullscreen player and mini player
document.addEventListener('DOMContentLoaded', () => {
  const fsPlayer = document.getElementById('fullscreen-player');
  const miniPlayer = document.getElementById('mini-player');
  
  // Fullscreen player gestures
  if (fsPlayer) {
    fsPlayer.addEventListener('touchstart', (e) => {
      // Don't interfere with controls
      if (e.target.closest('button') || e.target.closest('input')) return;
      
      touchStartX = e.changedTouches[0].screenX;
      touchStartY = e.changedTouches[0].screenY;
      
      // Double tap detection
      const currentTime = new Date().getTime();
      const tapLength = currentTime - lastTap;
      if (tapLength < 300 && tapLength > 0) {
        // Double tap detected - toggle play/pause
        togglePlay();
      }
      lastTap = currentTime;
    }, false);
    
    fsPlayer.addEventListener('touchend', (e) => {
      if (e.target.closest('button') || e.target.closest('input')) return;
      
      touchEndX = e.changedTouches[0].screenX;
      touchEndY = e.changedTouches[0].screenY;
      handleGesture();
    }, false);
  }
  
  // Mini player gestures
  if (miniPlayer) {
    miniPlayer.addEventListener('touchstart', (e) => {
      if (e.target.closest('button')) return;
      
      touchStartX = e.changedTouches[0].screenX;
      touchStartY = e.changedTouches[0].screenY;
    }, false);
    
    miniPlayer.addEventListener('touchend', (e) => {
      if (e.target.closest('button')) return;
      
      touchEndX = e.changedTouches[0].screenX;
      touchEndY = e.changedTouches[0].screenY;
      
      const deltaX = touchEndX - touchStartX;
      const minSwipeDistance = 50;
      
      if (Math.abs(deltaX) > minSwipeDistance) {
        if (deltaX > 0) {
          playPrev();
        } else {
          playNext();
        }
      }
    }, false);
  }
});
audio.preload = 'metadata'; // Preload metadata only

/* ─── $ helpers ─────────────────────────────── */
const $  = id => document.getElementById(id);
const $$ = sel => document.querySelectorAll(sel);

/* ══════════════════════════════════════════════
   AUTH
══════════════════════════════════════════════ */
async function checkAuth() {
  try {
    const r = await fetch(`${API}/api/auth/me`, { credentials: 'include' });
    const d = await r.json();
    d.user ? setUser(d.user) : setGuest();
  } catch { setGuest(); }
}

function setUser(u) {
  currentUser = u;
  const name = u.display_name || u.first_name || u.username || 'User';

  // Add authenticated class to body
  document.body.classList.add('authenticated');

  // sidebar
  $('auth-login').style.display = 'none';
  $('auth-user').style.display  = 'block';
  $('uname').textContent = name;
  if (u.photo_url) {
    $('user-photo').src = u.photo_url;
    $('user-photo').style.display = 'block';
    $('user-icon').style.display  = 'none';
  }

  // mobile header — hide login btn
  const mb = $('mob-login-btn');
  if (mb) mb.style.display = 'none';
  const mu = $('mob-user-btn');
  if (mu) mu.style.display = 'flex';
  const mn = $('mob-user-name');
  if (mn) mn.textContent = name;
}

function setGuest() {
  currentUser = null;
  
  // Remove authenticated class from body
  document.body.classList.remove('authenticated');
  
  $('auth-login').style.display = 'block';
  $('auth-user').style.display  = 'none';

  const mb = $('mob-login-btn');
  if (mb) mb.style.display = 'flex';
  const mu = $('mob-user-btn');
  if (mu) mu.style.display = 'none';
}

window.onTelegramAuth = async function(user) {
  closeTgModal();
  try {
    const r = await fetch(`${API}/api/auth/telegram`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(user),
    });
    const d = await r.json();
    if (d.success) { setUser(d.user); loadFavorites(); loadRecent(); }
    else showToast('Ошибка входа', 'error');
  } catch { showToast('Ошибка сети', 'error'); }
};

let tgWidgetTimeout = null;

// Check auth configuration before opening modal
async function checkAuthConfig() {
  try {
    const r = await fetch(`${API}/api/auth/debug`, { method: 'GET' });
    const text = await r.text();
    
    // Check if HTML response (404 page)
    if (text.trim().startsWith('<')) {
      console.warn('Auth debug endpoint returned HTML (404), skipping config check');
      return { config_status: 'unknown' };
    }
    
    const d = JSON.parse(text);
    console.log('Auth config check:', d);
    return d;
  } catch(e) {
    console.error('Failed to check auth config:', e);
    return { config_status: 'unknown' };
  }
}

async function openTgModal() {
  // Open local login modal
  $('modal-tg').classList.add('open');
  showLoginForm();
  return;
  
  // Old Telegram code removed
  const config = await checkAuthConfig();
  if (false && config && config.config_status !== 'ok') {
    console.warn('Auth configuration issue:', config);
    showToast('Проблема с настройкой авторизации. Проверьте консоль.', 'error', 5000);
  }
  
  const modal = $('modal-tg');
  modal.classList.add('open');
  
  // Clear and inject Telegram widget
  const container = $('tg-widget-container');
  const fallback = $('tg-fallback');
  container.innerHTML = '';
  
  // Show loading initially
  container.innerHTML = '<div style="padding:20px;color:var(--text3)"><div class="spinner" style="margin:0 auto 12px"></div>Загрузка...</div>';
  if (fallback) fallback.style.display = 'none';
  
  // Set timeout for fallback - reduced to 2 seconds
  tgWidgetTimeout = setTimeout(() => {
    container.innerHTML = '';
    if (fallback) fallback.style.display = 'block';
    console.log('Telegram widget load timeout, showing fallback');
  }, 2000); // 2 seconds timeout
  
  // Try Mini App auth first if available
  if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
    console.log('Attempting Mini App authentication...');
    authenticateWithMiniApp();
    closeTgModal();
    return;
  }
  
  // Create Telegram Login Widget script
  const script = document.createElement('script');
  script.async = true;
  script.src = 'https://telegram.org/js/telegram-widget.js?22';
  script.setAttribute('data-telegram-login', BOT);
  script.setAttribute('data-size', 'large');
  script.setAttribute('data-radius', '8');
  script.setAttribute('data-request-access', 'write');
  script.setAttribute('data-userpic', 'true');
  script.setAttribute('data-onauth', 'onTelegramAuth(user)');
  
  // On load success, clear timeout
  script.onload = () => {
    console.log('Telegram widget loaded successfully');
    if (tgWidgetTimeout) {
      clearTimeout(tgWidgetTimeout);
      tgWidgetTimeout = null;
    }
  };
  
  // On error, show fallback
  script.onerror = (e) => {
    console.error('Telegram widget failed to load:', e);
    container.innerHTML = '';
    if (fallback) fallback.style.display = 'block';
    if (tgWidgetTimeout) {
      clearTimeout(tgWidgetTimeout);
      tgWidgetTimeout = null;
    }
  };
  
  container.appendChild(script);
}
window.openTgModal = openTgModal;

// Direct Telegram fallback - opens Mini App
function openTelegramDirect() {
  // Close modal
  closeTgModal();
  
  // Check if we're already in Telegram WebApp
  if (window.Telegram && window.Telegram.WebApp) {
    console.log('Already in Telegram WebApp, using Mini App auth');
    authenticateWithMiniApp();
    return;
  }
  
  // Open Telegram bot with Mini App parameter
  // The bot should be configured with a Mini App that opens this website
  const tgUrl = `https://t.me/${BOT}?startapp=auth`;
  window.open(tgUrl, '_blank');
  
  showToast('Откройте бота и нажмите "Open App" для автоматической авторизации', 'info', 5000);
}
window.openTelegramDirect = openTelegramDirect;

// Authenticate using Mini App initData (works even when widget is blocked)
async function authenticateWithMiniApp() {
  if (!window.Telegram || !window.Telegram.WebApp) {
    showToast('Эта функция работает только внутри Telegram', 'error');
    return;
  }
  
  const tg = window.Telegram.WebApp;
  const initData = tg.initData;
  
  if (!initData) {
    showToast('Нет данных авторизации от Telegram', 'error');
    return;
  }
  
  console.log('Authenticating with Mini App initData');
  
  try {
    const r = await fetch(`${API}/api/auth/miniapp`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ initData: initData }),
    });
    
    const d = await r.json();
    
    if (d.success) {
      setUser(d.user);
      loadFavorites();
      loadRecent();
      showToast('Успешный вход через Telegram!');
      
      // If on profile page, reload it
      const profilePage = $('page-profile');
      if (profilePage && profilePage.classList.contains('active')) {
        loadProfile();
      }
    } else {
      showToast(d.error || 'Ошибка авторизации', 'error');
    }
  } catch (e) {
    console.error('MiniApp auth error:', e);
    showToast('Ошибка сети при авторизации', 'error');
  }
}
window.authenticateWithMiniApp = authenticateWithMiniApp;

// Check if we're in Telegram WebApp on page load and auto-auth
function checkTelegramWebApp() {
  // Telegram auth disabled - use local auth only
  console.log('Local authentication mode');
  return false;
}

function closeTgModal() { 
  $('modal-tg').classList.remove('open'); 
}
window.closeTgModal = closeTgModal;

// Local Auth Functions
function showLoginForm() {
  $('login-form').style.display = 'block';
  $('register-form').style.display = 'none';
  $('auth-title').textContent = 'Вход';
}
window.showLoginForm = showLoginForm;

function showRegisterForm() {
  $('login-form').style.display = 'none';
  $('register-form').style.display = 'block';
  $('auth-title').textContent = 'Регистрация';
}
window.showRegisterForm = showRegisterForm;

async function handleLogin() {
  const username = $('login-username').value.trim();
  const password = $('login-password').value.trim();
  
  if (!username || !password) {
    showToast('Введите логин и пароль', 'error');
    return;
  }
  
  try {
    const r = await fetch(`${API}/api/auth/login`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include',
      body: JSON.stringify({username, password})
    });
    
    const d = await r.json();
    
    if (r.ok && d.success) {
      setUser(d.user);
      loadFavorites();
      loadRecent();
      closeTgModal();
      showToast(`Добро пожаловать, ${d.user.display_name}!`);
      switchPage('home');
    } else {
      showToast(d.error || 'Ошибка входа', 'error');
    }
  } catch (e) {
    console.error('Login error:', e);
    showToast('Ошибка сети', 'error');
  }
}
window.handleLogin = handleLogin;

async function handleRegister() {
  const username = $('reg-username').value.trim();
  const password = $('reg-password').value.trim();
  const display_name = $('reg-display-name').value.trim();
  
  if (!username || username.length < 3) {
    showToast('Логин должен быть минимум 3 символа', 'error');
    return;
  }
  
  if (!password || password.length < 6) {
    showToast('Пароль должен быть минимум 6 символов', 'error');
    return;
  }
  
  try {
    const r = await fetch(`${API}/api/auth/register`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password, display_name})
    });
    
    const d = await r.json();
    
    if (r.ok && d.success) {
      showToast('Регистрация успешна! Войдите в систему');
      showLoginForm();
      $('login-username').value = username;
      $('login-password').value = password;
    } else {
      showToast(d.error || 'Ошибка регистрации', 'error');
    }
  } catch (e) {
    console.error('Register error:', e);
    showToast('Ошибка сети', 'error');
  }
}
window.handleRegister = handleRegister;

// Telegram Login callback
window.onTelegramAuth = async function(user) {
  console.log('Telegram auth callback received:', user);
  closeTgModal();
  
  // Validate user data
  if (!user || !user.id || !user.hash) {
    console.error('Invalid user data from Telegram:', user);
    showToast('Ошибка: неверные данные от Telegram', 'error');
    return;
  }
  
  console.log('User ID:', user.id);
  console.log('Username:', user.username);
  console.log('Auth date:', user.auth_date);
  console.log('Hash present:', !!user.hash);
  
  try {
    const r = await fetch(`${API}/api/auth/telegram`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(user),
    });
    
    console.log('Auth response status:', r.status);
    
    const d = await r.json();
    console.log('Auth response:', d);
    
    if (d.success) { 
      setUser(d.user); 
      loadFavorites(); 
      loadRecent();
      showToast('Успешный вход!');
      
      // If on profile page, reload it
      const profilePage = $('page-profile');
      if (profilePage && profilePage.classList.contains('active')) {
        loadProfile();
      }
    } else {
      const errorMsg = d.error || 'Ошибка входа';
      console.error('Auth failed:', errorMsg);
      showToast(errorMsg, 'error');
    }
  } catch(e) { 
    console.error('Telegram auth network error:', e);
    showToast('Ошибка сети. Проверьте консоль (F12) для деталей.', 'error'); 
  }
};

$('btn-login') && $('btn-login').addEventListener('click', openTgModal);
$('btn-logout') && $('btn-logout').addEventListener('click', async () => {
  await fetch(`${API}/api/auth/logout`, { method: 'POST', credentials: 'include' });
  setGuest();
  loadFavorites();
  loadRecent();
});

/* ══════════════════════════════════════════════
   NAVIGATION
══════════════════════════════════════════════ */
function switchPage(page, saveHistory = true) {
  // Save current page as previous (only for main pages, not artist/album)
  const currentPage = document.querySelector('.page.active');
  if (currentPage && saveHistory) {
    const currentId = currentPage.id.replace('page-', '');
    if (['home', 'search', 'favorites', 'offline', 'profile'].includes(currentId)) {
      previousPage = currentId;
    }
  }
  
  $$('.page').forEach(p => p.classList.remove('active'));
  $$('.nav-item').forEach(n => n.classList.remove('active'));
  const pg = $(`page-${page}`);
  if (pg) pg.classList.add('active');
  const nav = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (nav) nav.classList.add('active');
  if (isMobile) closeSidebar();
  if (page === 'favorites') loadFavorites();
  if (page === 'search') setTimeout(() => $('search-input') && $('search-input').focus(), 100);
  if (page === 'profile') loadProfile();
  if (page === 'offline') loadOfflineTracks();
  // scroll top
  const ms = $('main-scroll');
  if (ms) ms.scrollTop = 0;
}
window.switchPage = switchPage;

function goBack() {
  switchPage(previousPage, false);
}
window.goBack = goBack;

$$('.nav-item').forEach(el => {
  el.addEventListener('click', e => { e.preventDefault(); switchPage(el.dataset.page); });
});

/* ══════════════════════════════════════════════
   MOBILE SIDEBAR
══════════════════════════════════════════════ */
function openSidebar() {
  $('sidebar').classList.add('open');
  $('overlay').classList.add('show');
}
function closeSidebar() {
  $('sidebar').classList.remove('open');
  $('overlay').classList.remove('show');
}
window.openSidebar  = openSidebar;
window.closeSidebar = closeSidebar;

window.addEventListener('resize', () => {
  isMobile = window.innerWidth <= 768;
});

/* ══════════════════════════════════════════════
   SEARCH
══════════════════════════════════════════════ */
let searchTimer = null;

const searchInput = $('search-input');
if (searchInput) {
  searchInput.addEventListener('input', e => {
    const q = e.target.value.trim();
    const cx = $('search-clear');
    if (cx) cx.style.display = q ? 'block' : 'none';
    clearTimeout(searchTimer);
    if (q.length < 2) { showSearchEmpty(); return; }
    showSearchLoading();
    searchTimer = setTimeout(() => doSearch(q), 450);
  });
  searchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') { clearTimeout(searchTimer); doSearch(e.target.value.trim()); }
  });
}

function clearSearch() {
  if (searchInput) searchInput.value = '';
  const cx = $('search-clear');
  if (cx) cx.style.display = 'none';
  showSearchEmpty();
}
window.clearSearch = clearSearch;

function showSearchEmpty() {
  $('search-empty').style.display   = 'block';
  $('search-loading').style.display = 'none';
  $('search-tabs').style.display    = 'none';
  $('tab-tracks').innerHTML         = '';
  $('tab-artists').innerHTML        = '';
}
function showSearchLoading() {
  $('search-empty').style.display   = 'none';
  $('search-loading').style.display = 'flex';
  $('search-tabs').style.display    = 'none';
}

async function doSearch(q) {
  if (!q || q.length < 2) { showSearchEmpty(); return; }
  showSearchLoading();
  
  // Save to recent searches (device-specific)
  saveRecentSearch(q);
  
  try {
    const r = await fetch(`${API}/api/search/all?q=${encodeURIComponent(q)}`, { credentials: 'include' });
    const d = await r.json();
    searchData = { tracks: d.tracks || [], artists: d.artists || [] };
    $('search-loading').style.display = 'none';
    $('search-tabs').style.display    = 'flex';
    renderSearchTracks(searchData.tracks);
    renderArtists(searchData.artists);
    switchTab(activeTab);
  } catch(e) {
    console.error(e);
    $('search-loading').style.display = 'none';
    $('search-empty').style.display   = 'block';
    $('search-empty').querySelector('p').textContent = 'Ошибка поиска';
  }
}
window.doSearch = doSearch;

function renderSearchTracks(tracks) {
  const box = $('tab-tracks');
  if (!tracks.length) {
    box.innerHTML = '<div class="empty-state"><i class="fas fa-music"></i><p>Треки не найдены</p></div>';
    return;
  }
  playlist = tracks;
  box.innerHTML = tracks.map((t, i) => trackItemHTML(t, i)).join('');
  
  // Check favorite state for all tracks
  tracks.forEach(t => {
    if (t.id) checkFavState(t.id);
  });
}

function renderArtists(artists) {
  const box = $('tab-artists');
  if (!artists.length) {
    box.innerHTML = '<div class="empty-state"><i class="fas fa-user"></i><p>Исполнители не найдены</p></div>';
    return;
  }
  box.innerHTML = artists.map(a => `
    <div class="artist-card" onclick="openArtist('${esc(a.browseId)}','${esc(a.name)}')">
      <div class="artist-ava">
        ${a.cover ? `<img src="${esc(proxyImage(a.cover))}" alt="" loading="lazy">` : '<i class="fas fa-user"></i>'}
      </div>
      <div class="artist-name">${esc(a.name)}</div>
      ${a.subscribers ? `<div class="artist-subs">${esc(a.subscribers)}</div>` : ''}
    </div>`).join('');
}

/* ── Tabs ── */
$$('.tab').forEach(el => {
  el.addEventListener('click', () => switchTab(el.dataset.tab));
});
function switchTab(tab) {
  activeTab = tab;
  $$('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  $('tab-tracks').style.display  = tab === 'tracks'  ? 'flex' : 'none';
  $('tab-artists').style.display = tab === 'artists' ? 'grid' : 'none';
  $('tab-tracks').style.flexDirection = 'column';
}

/* ══════════════════════════════════════════════
   ARTIST PAGE
══════════════════════════════════════════════ */
async function openArtist(browseId, name) {
  switchPage('artist');
  $('artist-hero').innerHTML  = `<div class="loading-state"><div class="spinner"></div></div>`;
  $('artist-tracks').innerHTML = '';
  try {
    const r = await fetch(`${API}/api/artist/browse/${browseId}`, { credentials: 'include' });
    const d = await r.json();
    const bg = d.cover ? `style="background-image:url('${esc(d.cover)}')"` : '';
    $('artist-hero').innerHTML = `
      <div class="artist-hero-bg" ${bg}></div>
      <button class="btn-back" onclick="goBack()"><i class="fas fa-chevron-left"></i></button>
      <div class="artist-hero-content">
        <div class="artist-avatar">
          ${d.cover ? `<img src="${esc(proxyImage(d.cover))}" alt="${esc(d.name)}" class="artist-avatar-img">` : '<i class="fas fa-user-music"></i>'}
        </div>
        <div class="artist-hero-info">
          <div class="artist-label">Исполнитель</div>
          <h1 class="artist-name">${esc(d.name || name)}</h1>
          ${d.subscribers ? `
            <div class="artist-stats">
              <div class="artist-stat">
                <i class="fas fa-users"></i>
                <span>${esc(d.subscribers)}</span>
              </div>
              ${d.tracks && d.tracks.length ? `
                <div class="artist-stat">
                  <i class="fas fa-music"></i>
                  <span>${d.tracks.length} треков</span>
                </div>
              ` : ''}
              ${d.albums && d.albums.length ? `
                <div class="artist-stat">
                  <i class="fas fa-compact-disc"></i>
                  <span>${d.albums.length} альбомов</span>
                </div>
              ` : ''}
            </div>
          ` : ''}
          ${d.description ? `<p class="artist-description">${esc(d.description.slice(0,300))}${d.description.length > 300 ? '…' : ''}</p>` : ''}
        </div>
      </div>`;
    
    let tracksHTML = '';
    if (d.tracks && d.tracks.length) {
      playlist = d.tracks;
      tracksHTML = `
        <div style="padding:0 32px 8px;font-size:13px;color:var(--text3);font-weight:600;letter-spacing:.05em">ПОПУЛЯРНЫЕ ТРЕКИ</div>
        ${d.tracks.map((t, i) => trackItemHTML(t, i)).join('')}`;
      
      // Check favorite state for artist tracks
      setTimeout(() => {
        d.tracks.forEach(t => {
          if (t.id) checkFavState(t.id);
        });
      }, 100);
    }
    
    let albumsHTML = '';
    if (d.albums && d.albums.length) {
      albumsHTML = `
        <div style="padding:24px 32px 8px;font-size:13px;color:var(--text3);font-weight:600;letter-spacing:.05em">АЛЬБОМЫ</div>
        <div class="albums-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:16px;padding:0 32px 24px">
          ${d.albums.map(a => `
            <div class="album-card" onclick="openAlbum('${a.browseId}','${esc(a.title)}')" style="cursor:pointer">
              <div class="album-cover" style="width:140px;height:140px;border-radius:8px;overflow:hidden;background:var(--bg3);margin-bottom:8px">
                ${a.cover ? `<img src="${esc(proxyImage(a.cover))}" alt="" style="width:100%;height:100%;object-fit:cover">` : '<i class="fas fa-music" style="font-size:48px;color:var(--text3);display:flex;align-items:center;justify-content:center;height:100%"></i>'}
              </div>
              <div class="album-title" style="font-size:13px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.title)}</div>
              ${a.year ? `<div class="album-year" style="font-size:12px;color:var(--text3)">${esc(a.year)}</div>` : ''}
            </div>
          `).join('')}
        </div>`;
    }
    
    $('artist-tracks').innerHTML = tracksHTML + albumsHTML || '<div class="empty-state"><p>Контент не найден</p></div>';
  } catch(e) {
    $('artist-hero').innerHTML = `<div class="empty-state"><p>Ошибка загрузки</p></div>`;
  }
}
window.openArtist = openArtist;

async function openAlbum(browseId, title) {
  switchPage('album');
  
  // Temporary placeholder - albums not available yet
  $('album-hero').innerHTML = `
    <button class="btn-back" onclick="goBack()"><i class="fas fa-chevron-left"></i></button>
    <div class="album-hero-info">
      <div class="album-hero-cover">
        <i class="fas fa-music" style="font-size:64px;color:var(--text3);display:flex;align-items:center;justify-content:center;height:100%"></i>
      </div>
      <div class="album-hero-details">
        <h1>${esc(title)}</h1>
        <p style="color:var(--text3)">Альбом</p>
      </div>
    </div>`;
  
  $('album-tracks').innerHTML = `
    <div class="empty-state" style="padding:48px 32px">
      <i class="fas fa-compact-disc" style="font-size:64px;color:var(--text3);margin-bottom:16px"></i>
      <h3 style="font-size:20px;font-weight:700;margin-bottom:8px;color:var(--text)">Альбомы временно недоступны</h3>
      <p style="color:var(--text3);max-width:400px;margin:0 auto">
        Мы работаем над добавлением полной поддержки альбомов. 
        Пока вы можете слушать треки через поиск или страницы исполнителей.
      </p>
    </div>`;
  
  /* COMMENTED OUT - Will be enabled when album API is ready
  try {
    const r = await fetch(`${API}/api/album/${browseId}`, { credentials: 'include' });
    const d = await r.json();
    
    if (d.error) {
      showToast(d.error, 'error');
      return;
    }
    
    // Update album hero
    const coverHTML = d.cover ? `<img src="${esc(d.cover)}" alt="">` : '<i class="fas fa-music"></i>';
    $('album-hero').innerHTML = `
      <button class="btn-back" onclick="goBack()"><i class="fas fa-chevron-left"></i></button>
      <div class="album-hero-info">
        <div class="album-hero-cover">${coverHTML}</div>
        <div class="album-hero-details">
          <h1>${esc(d.title)}</h1>
          <p>${esc(d.artist)}</p>
          ${d.year ? `<p style="color:var(--text3)">${esc(d.year)}</p>` : ''}
        </div>
      </div>`;
    
    // Display tracks
    if (d.tracks && d.tracks.length) {
      playlist = d.tracks;
      $('album-tracks').innerHTML = `
        <div style="padding:0 32px 8px;font-size:13px;color:var(--text3);font-weight:600;letter-spacing:.05em">ТРЕКИ</div>
        ${d.tracks.map((t, i) => trackItemHTML(t, i)).join('')}`;
      
      setTimeout(() => {
        d.tracks.forEach(t => {
          if (t.id) checkFavState(t.id);
        });
      }, 100);
    } else {
      $('album-tracks').innerHTML = '<div class="empty-state"><p>Треки не найдены</p></div>';
    }
  } catch(e) {
    $('album-hero').innerHTML = `<div class="empty-state"><p>Ошибка загрузки альбома</p></div>`;
  }
  */
}
window.openAlbum = openAlbum;

/* ══════════════════════════════════════════════
   TRACK ITEM HTML
══════════════════════════════════════════════ */
function trackItemHTML(t, i) {
  const cover  = t.cover ? `<img src="${esc(proxyImage(t.cover))}" alt="" loading="lazy">` : '<i class="fas fa-music"></i>';
  const dur    = t.duration_ms ? fmtMs(t.duration_ms) : '';
  const isNow  = t.id === currentId;
  return `
  <div class="track-item${isNow ? ' playing' : ''}" data-idx="${i}" data-id="${esc(t.id)}" onclick="playFromList(${i})">
    <div class="t-num">
      <span class="num-txt">${i + 1}</span>
      <i class="fas fa-play play-ico"></i>
    </div>
    <div class="t-cover">${cover}</div>
    <div class="t-info">
      <div class="t-title">${esc(t.title)}</div>
      <div class="t-artist">${esc(t.artist || '')}</div>
    </div>
    <div class="t-dur">${dur}</div>
    <div class="t-actions">
      <button class="t-btn" title="Избранное" onclick="event.stopPropagation();toggleFav('${esc(t.id)}',this)">
        <i class="far fa-heart"></i>
      </button>
      <button class="t-btn" title="Скачать" onclick="event.stopPropagation();dlTrack('${esc(t.id)}')">
        <i class="fas fa-download"></i>
      </button>
      <button class="t-btn" title="Текст" onclick="event.stopPropagation();showLyrics('${esc(t.id)}','${esc(t.title)}','${esc(t.artist||'')}')">
        <i class="fas fa-align-left"></i>
      </button>
    </div>
  </div>`;
}

/* ══════════════════════════════════════════════
   PLAYER
══════════════════════════════════════════════ */
function playFromList(idx) {
  playIdx = idx;
  const t = playlist[idx];
  if (t) loadAndPlay(t);
}
window.playFromList = playFromList;

function loadAndPlay(t) {
  currentId = t.id;
  updatePlayerUI(t);
  highlightPlaying();
  
  // Update fullscreen player if open or when opening
  if (isFullscreenPlayerOpen) {
    updateFullscreenPlayerUI();
  }
  
  // Update Media Session metadata for lock screen
  updateMediaSessionMetadata();

  // Always fetch fresh stream URL (no cache)
  tryYtEmbed(t);
}

// Track retry attempts to avoid infinite loops
const retryAttempts = new Map();

async function tryYtEmbed(t) {
  try {
    // Check offline cache first
    const offlineData = await getOfflineTrack(t.id);
    if (offlineData && offlineData.audio) {
      console.log('Playing from offline cache');
      const blobUrl = URL.createObjectURL(offlineData.audio);
      await loadAudioSource(blobUrl);
      setPlaying(true);
      showLoadingIndicator(false);
      return;
    }
    
    // Check retry attempts
    const attempts = retryAttempts.get(t.id) || 0;
    if (attempts >= 2) {
      console.log('Max retry attempts reached, showing fallback');
      retryAttempts.delete(t.id);
      showLoadingIndicator(false);
      openYTFallback(t);
      return;
    }
    
    showLoadingIndicator(true);
    
    // Use proxied stream URL directly
    const streamUrl = `${API}/api/track/${t.id}/stream`;
    
    try {
      await loadAudioSource(streamUrl);
      setPlaying(true);
      showLoadingIndicator(false);
      retryAttempts.delete(t.id);
    } catch (playErr) {
      console.error('Play error:', playErr);
      showLoadingIndicator(false);
      
      // Try to get user interaction for autoplay
      if (playErr.name === 'NotAllowedError') {
        showToast('Нажмите Play для воспроизведения', 'info');
      } else if (playErr.name === 'NotSupportedError' || playErr.name === 'AbortError') {
        // Stream failed, retry
        console.log('Stream failed, retrying...');
        const newAttempts = attempts + 1;
        if (newAttempts < 2) {
          retryAttempts.set(t.id, newAttempts);
          setTimeout(() => tryYtEmbed(t), 1500);
        } else {
          openYTFallback(t);
        }
      } else {
        openYTFallback(t);
      }
    }
  } catch (err) {
    console.error('Stream error:', err);
    showLoadingIndicator(false);
    retryAttempts.delete(t.id);
    openYTFallback(t);
  }
}

// Helper function to load and play audio
async function loadAudioSource(url) {
  return new Promise((resolve, reject) => {
    audio.src = url;
    audio.load();
    
    const playPromise = audio.play();
    if (playPromise !== undefined) {
      playPromise.then(resolve).catch(reject);
    } else {
      resolve();
    }
  });
}

// Show/hide loading indicator
function showLoadingIndicator(show) {
  const pLoading = $('p-loading');
  const miniLoading = $('mini-loading');
  
  if (pLoading) pLoading.style.display = show ? 'block' : 'none';
  if (miniLoading) miniLoading.style.display = show ? 'block' : 'none';
}

// Preload next track in background
async function preloadNextTrack() {
  // Preloading disabled - always fetch fresh URLs
  return;
}

function updatePlayerUI(t) {
  if (!t) return;
  
  // desktop player
  const pl = $('player');
  if (pl) pl.style.display = 'grid';

  setText('p-title',  t.title  || '—');
  setText('p-artist', t.artist || '—');
  
  // Handle cover image
  const pc = $('p-cover');
  const pp = document.querySelector('.p-cover-placeholder');
  if (t.cover) {
    if (pc) { pc.src = proxyImage(t.cover); pc.style.display = 'block'; }
    if (pp) pp.style.display = 'none';
  } else {
    if (pc) pc.style.display = 'none';
    if (pp) pp.style.display = 'flex';
  }

  // mini player (mobile)
  const mp = $('mini-player');
  if (mp) mp.style.display = 'flex';
  setText('mini-title',  t.title  || '—');
  setText('mini-artist', t.artist || '—');
  const mc = $('mini-cover');
  if (mc) { mc.src = proxyImage(t.cover) || ''; mc.style.display = t.cover ? 'block' : 'none'; }
}

// Mobile: expand mini-player to show full player or track details
function expandPlayer() {
  if (currentId && playlist[playIdx]) {
    // Show toast with current track info on mobile
    const t = playlist[playIdx];
    showToast(`Сейчас играет: ${t.title} — ${t.artist || ''}`, 'info');
  }
}
window.expandPlayer = expandPlayer;

function setText(id, val) { const el = $(id); if (el) el.textContent = val; }

function setPlaying(v) {
  isPlaying = v;
  const icon = v ? 'fa-pause' : 'fa-play';
  const pp = $('p-play');
  if (pp) pp.querySelector('i').className = `fas ${icon}`;
  const mp = $('mini-play');
  if (mp) mp.querySelector('i').className = `fas ${icon}`;
  
  // Update fullscreen player
  updateFullscreenPlayButton();
}

function togglePlay() {
  if (!audio.src && !currentId) return;
  isPlaying ? audio.pause() : audio.play().catch(() => {});
}
window.togglePlay = togglePlay;

function prevTrack() { if (playIdx > 0) playFromList(playIdx - 1); }
function nextTrack() { if (playIdx < playlist.length - 1) playFromList(playIdx + 1); }
window.prevTrack = prevTrack;
window.nextTrack = nextTrack;

// Shuffle and repeat state
let isShuffleOn = false;
let isRepeatOn = false;

function toggleShuffle() {
  isShuffleOn = !isShuffleOn;
  const btn = $('fs-shuffle');
  if (btn) btn.classList.toggle('active', isShuffleOn);
  showToast(isShuffleOn ? 'Shuffle включен' : 'Shuffle выключен', 'info');
}
window.toggleShuffle = toggleShuffle;

function toggleRepeat() {
  isRepeatOn = !isRepeatOn;
  const btn = $('fs-repeat');
  if (btn) btn.classList.toggle('active', isRepeatOn);
  showToast(isRepeatOn ? 'Repeat включен' : 'Repeat выключен', 'info');
}
window.toggleRepeat = toggleRepeat;

// Modify nextTrack to respect shuffle/repeat
const originalNextTrack = nextTrack;
nextTrack = function() {
  if (isRepeatOn && currentId) {
    // Repeat current track
    playFromList(playIdx);
    return;
  }
  
  if (isShuffleOn && playlist.length > 1) {
    // Play random track (not current)
    let newIdx;
    do {
      newIdx = Math.floor(Math.random() * playlist.length);
    } while (newIdx === playIdx && playlist.length > 1);
    playFromList(newIdx);
    return;
  }
  
  // Default behavior
  if (playIdx < playlist.length - 1) {
    playFromList(playIdx + 1);
  } else if (playlist.length > 0) {
    // Loop to start if at end
    playFromList(0);
  }
};

// Handle track ended with repeat
audio.addEventListener('ended', () => {
  if (isRepeatOn) {
    playFromList(playIdx);
  } else {
    nextTrack();
  }
});

/* ── Player controls ── */
const pPlay = $('p-play');
const pPrev = $('p-prev');
const pNext = $('p-next');
const pSeek = $('p-seek');
const pVol  = $('p-vol');
const pFav  = $('p-fav');
const pDl   = $('p-dl');
const pLyr  = $('p-lyrics');

if (pPlay) pPlay.addEventListener('click', togglePlay);
if (pPrev) pPrev.addEventListener('click', prevTrack);
if (pNext) pNext.addEventListener('click', nextTrack);
if (pVol)  pVol.addEventListener('input',  e => { audio.volume = e.target.value / 100; });
if (pFav)  pFav.addEventListener('click',  () => currentId && toggleFav(currentId, pFav));
if (pDl)   pDl.addEventListener('click',   () => currentId && dlTrack(currentId));
if (pLyr)  pLyr.addEventListener('click',  () => {
  if (!currentId) return;
  const t = playlist.find(x => x.id === currentId) || {};
  showLyrics(currentId, t.title || '', t.artist || '');
});

/* ── Seek bar ── */
if (pSeek) {
  pSeek.addEventListener('input', e => {
    if (audio.duration) audio.currentTime = (e.target.value / 1000) * audio.duration;
  });
}

/* ── Progress bar click ── */
const pBar = $('p-bar');
if (pBar) {
  pBar.addEventListener('click', e => {
    const rect = pBar.getBoundingClientRect();
    const pct  = (e.clientX - rect.left) / rect.width;
    if (audio.duration) audio.currentTime = pct * audio.duration;
  });
}

/* ── Audio events ── */
audio.addEventListener('play',  () => setPlaying(true));
audio.addEventListener('pause', () => setPlaying(false));
audio.addEventListener('ended', () => {
  setPlaying(false);
  if (playIdx < playlist.length - 1) playFromList(playIdx + 1);
});
audio.addEventListener('timeupdate', () => {
  if (!audio.duration) return;
  const pct = audio.currentTime / audio.duration;
  const fill = $('p-fill');
  const dot  = $('p-dot');
  const seek = $('p-seek');
  const miniProgress = $('mini-progress-bg');
  
  if (fill) fill.style.width = (pct * 100) + '%';
  if (dot)  dot.style.left   = (pct * 100) + '%';
  if (seek) seek.value = Math.round(pct * 1000);
  if (miniProgress) miniProgress.style.width = (pct * 100) + '%';
  
  setText('p-cur', fmtSec(audio.currentTime));
  setText('p-dur', fmtSec(audio.duration));
});
audio.addEventListener('error', (e) => {
  console.error('Audio error:', e);
  const t = playlist[playIdx];
  if (t) {
    // Try to get fresh stream URL
    console.log('Stream failed, trying to get fresh URL...');
    
    // Retry with fresh URL
    setTimeout(() => {
      tryYtEmbed(t);
    }, 500);
  }
});

// Handle network errors during playback
audio.addEventListener('stalled', () => {
  console.warn('Audio stalled - buffering...');
  // Removed buffering toast
});

audio.addEventListener('waiting', () => {
  // Silent waiting
});

audio.addEventListener('canplay', () => {
  console.log('Audio can play');
});

audio.addEventListener('loadeddata', () => {
  console.log('Audio data loaded');
});

audio.addEventListener('loadstart', () => {
  // Silent load start
});

function highlightPlaying() {
  $$('.track-item').forEach(el => {
    el.classList.toggle('playing', el.dataset.id === currentId);
  });
}

/* ══════════════════════════════════════════════
   FAVOURITES
══════════════════════════════════════════════ */
let favSet = new Set();

async function loadFavorites() {
  const box = $('fav-list');
  if (box) box.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';

  try {
    const r = await fetch(`${API}/api/favorites`, { credentials: 'include' });
    const d = await r.json();
    favSet = new Set((d.tracks || []).map(t => t.id));

    if (!d.tracks || !d.tracks.length) {
      if (box) box.innerHTML = `
        <div class="empty-state">
          <i class="fas fa-heart"></i>
          <p>${currentUser ? 'Нет избранных треков' : 'Войдите, чтобы видеть избранное'}</p>
          ${!currentUser ? `<button class="btn-primary" style="margin-top:16px" onclick="openTgModal()"><i class="fab fa-telegram"></i> Войти</button>` : ''}
        </div>`;
      // home favs
      const hf = $('home-favs');
      if (hf) hf.innerHTML = '';
      return;
    }

    playlist = d.tracks;
    const html = d.tracks.map((t, i) => trackItemHTML(t, i)).join('');
    if (box) box.innerHTML = html;

    // home favs (first 5)
    const hf = $('home-favs');
    if (hf) hf.innerHTML = d.tracks.slice(0, 5).map((t, i) => trackItemHTML(t, i)).join('');

    updateFavIcons();
  } catch {
    if (box) box.innerHTML = '<div class="empty-state"><p>Ошибка загрузки</p></div>';
  }
}

async function toggleFav(trackId, btn) {
  if (!trackId) return;
  
  // Check authorization
  if (!currentUser) {
    showToast('Войдите в аккаунт, чтобы добавлять треки в избранное', 'error');
    return;
  }
  
  const isFav = favSet.has(trackId);
  const newState = !isFav;
  
  // Optimistic UI update
  if (newState) {
    favSet.add(trackId);
  } else {
    favSet.delete(trackId);
  }
  updateFavIcons();
  
  try {
    const r = await fetch(`${API}/api/favorites/${trackId}`, {
      method: newState ? 'POST' : 'DELETE',
      credentials: 'include',
    });
    
    if (!r.ok) {
      // Revert on error
      if (newState) {
        favSet.delete(trackId);
      } else {
        favSet.add(trackId);
      }
      updateFavIcons();
      showToast('Ошибка', 'error');
      return;
    }
    
    // Reload favorites page if active
    if ($('page-favorites').classList.contains('active')) {
      loadFavorites();
    }
    
    showToast(newState ? 'Добавлено в избранное' : 'Удалено из избранного');
  } catch (e) {
    console.error('toggleFav error:', e);
    // Revert on error
    if (newState) {
      favSet.delete(trackId);
    } else {
      favSet.add(trackId);
    }
    updateFavIcons();
    showToast('Ошибка сети', 'error');
  }
}
window.toggleFav = toggleFav;

async function checkFavState(trackId) {
  try {
    const r  = await fetch(`${API}/api/track/${trackId}`, { credentials: 'include' });
    const d  = await r.json();
    const on = d.is_favorite;
    if (on) favSet.add(trackId); else favSet.delete(trackId);
    updateFavIcons();
  } catch {}
}

function updateFavIcons() {
  // Update player button
  if (pFav && currentId) {
    const isFav = favSet.has(currentId);
    if (isFav) {
      pFav.classList.add('fav-on');
    } else {
      pFav.classList.remove('fav-on');
    }
    const icon = pFav.querySelector('i');
    if (icon) {
      icon.className = isFav ? 'fas fa-heart' : 'far fa-heart';
    }
  }

  // Update fullscreen player button
  const fsFav = $('fs-fav');
  if (fsFav && currentId) {
    const isFav = favSet.has(currentId);
    if (isFav) {
      fsFav.classList.add('active');
    } else {
      fsFav.classList.remove('active');
    }
    const icon = fsFav.querySelector('i');
    if (icon) {
      icon.className = isFav ? 'fas fa-heart' : 'far fa-heart';
    }
  }

  // Update track list buttons
  $$('.track-item').forEach(el => {
    const id = el.dataset.id;
    if (!id) return;
    
    const btn = el.querySelector('.t-btn');
    if (btn) {
      const isFav = favSet.has(id);
      if (isFav) {
        btn.classList.add('fav-on');
      } else {
        btn.classList.remove('fav-on');
      }
      const icon = btn.querySelector('i');
      if (icon) {
        icon.className = isFav ? 'fas fa-heart' : 'far fa-heart';
      }
    }
  });
}

/* ══════════════════════════════════════════════
   DOWNLOAD
══════════════════════════════════════════════ */
async function dlTrack(trackId) {
  showToast('Подготовка загрузки…', 'info');
  try {
    const r = await fetch(`${API}/api/track/${trackId}/download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ codec: 'mp3' }),
    });
    const d = await r.json();
    if (d.download_url) {
      const a = document.createElement('a');
      a.href = d.download_url; a.download = ''; a.click();
      showToast('Загрузка начата');
    } else { showToast('Ошибка загрузки', 'error'); }
  } catch { showToast('Ошибка сети', 'error'); }
}
window.dlTrack = dlTrack;

/* ══════════════════════════════════════════════
   LYRICS
══════════════════════════════════════════════ */
function formatLyrics(lyrics) {
  if (!lyrics) return '';
  
  // Detect and format lyrics sections (Intro, Verse, Chorus, etc.)
  const lines = lyrics.split('\n');
  let html = '<div class="lyrics-content">';
  let inSection = false;
  
  for (const line of lines) {
    const trimmed = line.trim();
    
    // Detect section headers like [Chorus], [Verse 1], [Intro], etc.
    if (trimmed.startsWith('[') && trimmed.endsWith(']')) {
      if (inSection) html += '</div>';
      const sectionName = trimmed.slice(1, -1);
      html += `<div class="lyrics-section"><div class="lyrics-section-title">${esc(sectionName)}</div>`;
      inSection = true;
    }
    // Empty lines - close current section
    else if (!trimmed) {
      if (inSection) {
        html += '</div>';
        inSection = false;
      }
    }
    // Regular lyrics line
    else {
      if (!inSection) html += '<div class="lyrics-section">';
      html += `<div class="lyrics-line">${esc(trimmed)}</div>`;
      inSection = true;
    }
  }
  
  if (inSection) html += '</div>';
  html += '</div>';
  
  return html;
}

async function showLyrics(trackId, title, artist) {
  const headTitle = title || 'Текст песни';
  const headArtist = artist || '';
  
  $('lyrics-head').innerHTML = `
    <div class="lyrics-header">
      <div class="lyrics-title">${esc(headTitle)}</div>
      ${headArtist ? `<div class="lyrics-artist">${esc(headArtist)}</div>` : ''}
    </div>
  `;
  $('lyrics-body').innerHTML   = '<div class="loading-state"><div class="spinner"></div></div>';
  $('modal-lyrics').classList.add('open');

  try {
    const r = await fetch(`${API}/api/track/${trackId}/lyrics`, { credentials: 'include' });
    const d = await r.json();
    if (d.lyrics) {
      $('lyrics-body').innerHTML = formatLyrics(d.lyrics);
    } else {
      $('lyrics-body').innerHTML = `
        <div class="empty-state">
          <i class="fas fa-file-alt" style="font-size:48px;margin-bottom:16px;color:var(--text3)"></i>
          <p>Текст песни не найден</p>
          <p style="font-size:12px;color:var(--text3);margin-top:8px;">Попробуйте найти текст на Genius.com</p>
        </div>`;
    }
  } catch(e) {
    console.error('Lyrics error:', e);
    $('lyrics-body').innerHTML = `
      <div class="empty-state">
        <i class="fas fa-exclamation-circle" style="font-size:48px;margin-bottom:16px;color:var(--red)"></i>
        <p>Ошибка загрузки текста</p>
      </div>`;
  }
}
window.showLyrics = showLyrics;

document.querySelector('#modal-lyrics .modal-close') &&
  document.querySelector('#modal-lyrics .modal-close').addEventListener('click', () => {
    $('modal-lyrics').classList.remove('open');
  });

/* ══════════════════════════════════════════════
   RECENT QUERIES
══════════════════════════════════════════════ */
function loadRecent() {
  try {
    const box = $('recent-queries');
    if (!box) return;
    
    // Load from localStorage (device-specific)
    const stored = localStorage.getItem('recentSearches');
    const qs = stored ? JSON.parse(stored).slice(0, 8) : [];
    
    if (!qs.length) { 
      box.innerHTML = '<span style="color:var(--text3);font-size:13px">Нет истории</span>'; 
      return; 
    }
    
    box.innerHTML = qs.map(q => `
      <div class="pill" onclick="switchPage('search');
        document.getElementById('search-input').value='${esc(q)}';
        document.getElementById('search-clear').style.display='block';
        doSearch('${esc(q)}')">${esc(q)}</div>`).join('');
  } catch {}
}

function saveRecentSearch(query) {
  try {
    if (!query || query.length < 2) return;
    
    const stored = localStorage.getItem('recentSearches');
    let searches = stored ? JSON.parse(stored) : [];
    
    // Remove duplicates and add to front
    searches = searches.filter(q => q !== query);
    searches.unshift(query);
    
    // Keep only last 20
    searches = searches.slice(0, 20);
    
    localStorage.setItem('recentSearches', JSON.stringify(searches));
    loadRecent();
  } catch {}
}

/* ══════════════════════════════════════════════
   TOAST
══════════════════════════════════════════════ */
let toastTimer = null;
function showToast(msg, type = 'success', onClick = null) {
  let box = $('toast');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toast';
    Object.assign(box.style, {
      position:'fixed', bottom:'110px', left:'50%', transform:'translateX(-50%)',
      padding:'10px 20px', borderRadius:'24px', fontSize:'14px', fontWeight:'600',
      zIndex:'9999', pointerEvents:'none', transition:'opacity .3s', opacity:'0',
      maxWidth:'320px', textAlign:'center', boxShadow:'0 4px 20px rgba(0,0,0,0.5)',
    });
    document.body.appendChild(box);
  }
  box.textContent = msg;
  box.style.background = type === 'error' ? '#e63946' : type === 'info' ? '#2AABEE' : '#1db954';
  box.style.color  = '#fff';
  box.style.opacity = '1';
  box.style.pointerEvents = onClick ? 'auto' : 'none';
  if (onClick) box.onclick = onClick;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.style.opacity = '0'; }, 3000);
}

/* ══════════════════════════════════════════════
   MODAL close on bg click
══════════════════════════════════════════════ */
$$('.modal-bg').forEach(el => {
  el.addEventListener('click', e => {
    if (e.target === el) el.classList.remove('open');
  });
});

/* ══════════════════════════════════════════════
   PROFILE PAGE
══════════════════════════════════════════════ */
async function loadProfile() {
  const guestSection = $('profile-guest');
  const authedSection = $('profile-authed');
  
  if (!currentUser) {
    guestSection.style.display = 'block';
    authedSection.style.display = 'none';
    return;
  }
  
  guestSection.style.display = 'none';
  authedSection.style.display = 'block';
  
  // Update profile header
  const name = currentUser.display_name || currentUser.first_name || currentUser.username || 'Пользователь';
  $('profile-name').textContent = name;
  $('profile-email').textContent = currentUser.email || currentUser.username || '';
  
  // Show account type without provider badge
  const accountType = currentUser.auth_type === 'local' ? 'Локальный аккаунт' : 'Аккаунт';
  $('profile-provider').innerHTML = `<span style="color:var(--text3);font-size:14px">${accountType}</span>`;
  
  // Update avatar
  const photoEl = $('profile-photo');
  const placeholderEl = $('profile-photo-placeholder');
  if (currentUser.photo_url) {
    photoEl.src = currentUser.photo_url;
    photoEl.style.display = 'block';
    placeholderEl.style.display = 'none';
  } else {
    photoEl.style.display = 'none';
    placeholderEl.style.display = 'flex';
  }
  
  // Load stats
  try {
    const r = await fetch(`${API}/api/user/profile`, { credentials: 'include' });
    const d = await r.json();
    
    if (d.stats) {
      $('stat-plays').textContent = d.stats.total_plays || 0;
      $('stat-favorites').textContent = d.stats.favorites_count || 0;
      $('stat-searches').textContent = d.stats.search_count || 0;
      $('stat-unique').textContent = d.stats.unique_tracks || 0;
    }
    
    // Add member since date
    if (d.user && d.user.created_at) {
      const memberSince = $('member-since');
      if (memberSince) {
        const date = new Date(d.user.created_at);
        const options = { year: 'numeric', month: 'long', day: 'numeric' };
        memberSince.textContent = `Участник с ${date.toLocaleDateString('ru-RU', options)}`;
      }
    }
    
    // Load history
    const historyBox = $('profile-history');
    const historyEmpty = $('profile-history-empty');
    
    if (d.recent_history && d.recent_history.length > 0) {
      historyBox.innerHTML = d.recent_history.map((h, i) => `
        <div class="track-item" style="grid-template-columns: 40px 1fr auto">
          <div class="t-num">${i + 1}</div>
          <div class="t-info">
            <div class="t-title">${esc(h.track_title)}</div>
            <div class="t-artist">${esc(h.artist_name || '')}</div>
          </div>
          <div style="font-size:12px;color:var(--text3)">${formatDate(h.played_at)}</div>
        </div>
      `).join('');
      historyBox.style.display = 'flex';
      historyEmpty.style.display = 'none';
    } else {
      historyBox.style.display = 'none';
      historyEmpty.style.display = 'block';
    }
  } catch(e) {
    console.error('Profile load error:', e);
  }
}
window.loadProfile = loadProfile;

function formatDate(isoString) {
  if (!isoString) return '';
  try {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);
    
    if (diffMins < 1) return 'только что';
    if (diffMins < 60) return `${diffMins} мин. назад`;
    if (diffHours < 24) return `${diffHours} ч. назад`;
    if (diffDays < 7) return `${diffDays} дн. назад`;
    
    return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
  } catch { return ''; }
}

async function logout() {
  try {
    await fetch(`${API}/api/auth/logout`, { method: 'POST', credentials: 'include' });
    setGuest();
    showToast('Вы вышли из аккаунта');
    // Reload profile page
    if ($('page-profile').classList.contains('active')) {
      loadProfile();
    }
  } catch(e) {
    console.error('Logout error:', e);
  }
}
window.logout = logout;

/* ══════════════════════════════════════════════
   FULLSCREEN PLAYER
══════════════════════════════════════════════ */
let isFullscreenPlayerOpen = false;

function openFullscreenPlayer() {
  const fs = $('fullscreen-player');
  if (!fs || !currentId) return;
  
  isFullscreenPlayerOpen = true;
  fs.classList.add('active');
  fs.classList.remove('closing');
  
  // Hide other players
  const desktopPlayer = document.querySelector('.player');
  const miniPlayer = $('mini-player');
  const bottomNav = document.querySelector('.bottom-nav');
  
  if (desktopPlayer) desktopPlayer.style.display = 'none';
  if (miniPlayer) miniPlayer.style.display = 'none';
  if (bottomNav) bottomNav.style.display = 'none';
  
  // Update fullscreen player UI
  updateFullscreenPlayerUI();
  
  // Setup progress listener
  setupFullscreenProgress();
  
  // Prevent body scroll and hide other UI
  document.body.style.overflow = 'hidden';
  document.body.classList.add('fs-open');
}
window.openFullscreenPlayer = openFullscreenPlayer;

function closeFullscreenPlayer() {
  const fs = $('fullscreen-player');
  if (!fs) return;
  
  fs.classList.add('closing');
  isFullscreenPlayerOpen = false;
  
  // Restore other players
  const desktopPlayer = document.querySelector('.player');
  const miniPlayer = $('mini-player');
  const bottomNav = document.querySelector('.bottom-nav');
  
  // Check if mobile to show correct player
  const isMobile = window.innerWidth <= 768;
  
  if (desktopPlayer) desktopPlayer.style.display = '';
  if (miniPlayer) miniPlayer.style.display = isMobile ? 'flex' : '';
  if (bottomNav) bottomNav.style.display = isMobile ? 'flex' : '';
  
  setTimeout(() => {
    fs.classList.remove('active', 'closing');
    document.body.style.overflow = '';
    document.body.classList.remove('fs-open');
  }, 300);
}
window.closeFullscreenPlayer = closeFullscreenPlayer;

function updateFullscreenPlayerUI() {
  if (!currentId || !playlist[playIdx]) return;
  
  const t = playlist[playIdx];
  
  // Title and artist
  $('fs-title').textContent = t.title || '—';
  $('fs-artist').textContent = t.artist || '—';
  
  // Cover
  const coverEl = $('fs-cover');
  const bgEl = $('fs-bg-img');
  const placeholderEl = $('fs-cover-placeholder');
  
  if (t.cover) {
    coverEl.src = proxyImage(t.cover);
    coverEl.style.display = 'block';
    bgEl.src = proxyImage(t.cover);
    bgEl.style.display = 'block';
    placeholderEl.style.display = 'none';
  } else {
    coverEl.style.display = 'none';
    bgEl.style.display = 'none';
    placeholderEl.style.display = 'flex';
  }
  
  // Duration
  const audio = $('audio');
  if (audio && audio.duration) {
    $('fs-duration').textContent = fmtSec(audio.duration);
    $('fs-progress').max = audio.duration;
  } else if (t.duration_ms) {
    $('fs-duration').textContent = fmtMs(t.duration_ms);
    $('fs-progress').max = t.duration_ms / 1000;
  }
  
  // Favorite status
  updateFullscreenFavStatus();
  
  // Play button
  updateFullscreenPlayButton();
}

function updateFullscreenPlayButton() {
  const audio = $('audio');
  const btn = $('fs-play');
  if (!audio || !btn) return;
  
  const icon = audio.paused ? 'fa-play' : 'fa-pause';
  btn.innerHTML = `<i class="fas ${icon}"></i>`;
}

function updateFullscreenFavStatus() {
  if (!currentId) return;
  const btn = $('fs-fav');
  if (!btn) return;
  
  const isFav = favSet.has(currentId);
  const icon = btn.querySelector('i');
  if (icon) {
    icon.className = isFav ? 'fas fa-heart' : 'far fa-heart';
  }
  if (isFav) {
    btn.classList.add('active');
  } else {
    btn.classList.remove('active');
  }
}

function setupFullscreenProgress() {
  const audio = $('audio');
  const progress = $('fs-progress');
  if (!audio || !progress) return;
  
  // Update progress from audio time
  const updateProgress = () => {
    if (audio.duration) {
      progress.value = audio.currentTime;
      $('fs-current-time').textContent = fmtSec(audio.currentTime);
      
      // Update progress bar background using CSS variable
      const percent = (audio.currentTime / audio.duration) * 100;
      progress.style.setProperty('--progress-percent', `${percent}%`);
    }
  };
  
  audio.removeEventListener('timeupdate', updateProgress);
  audio.addEventListener('timeupdate', updateProgress);
  
  // Seek on input
  progress.oninput = (e) => {
    const time = parseFloat(e.target.value);
    audio.currentTime = time;
    $('fs-current-time').textContent = fmtSec(time);
  };
}

function toggleCurrentFav() {
  if (!currentId) return;
  toggleFav(currentId);
}
window.toggleCurrentFav = toggleCurrentFav;

function openFullscreenLyrics() {
  if (!currentId || !playlist[playIdx]) return;
  const t = playlist[playIdx];
  showLyrics(currentId, t.title, t.artist);
}
window.openFullscreenLyrics = openFullscreenLyrics;

function shareTrack() {
  if (!currentId || !playlist[playIdx]) return;
  const t = playlist[playIdx];
  
  const shareData = {
    title: t.title,
    text: `Слушаю ${t.title} — ${t.artist || ''} в Zvonko Music`,
    url: window.location.href
  };
  
  if (navigator.share) {
    navigator.share(shareData).catch(() => {});
  } else {
    // Fallback: copy to clipboard
    navigator.clipboard.writeText(`${shareData.text}\n${shareData.url}`).then(() => {
      showToast('Ссылка скопирована');
    });
  }
}
window.shareTrack = shareTrack;

function showPlayerMenu() {
  if (!currentId || !playlist[playIdx]) {
    showToast('Нет активного трека', 'error');
    return;
  }
  
  const track = playlist[playIdx];
  
  // Create menu overlay
  const overlay = document.createElement('div');
  overlay.className = 'menu-overlay';
  overlay.onclick = () => overlay.remove();
  
  const menu = document.createElement('div');
  menu.className = 'player-menu';
  menu.onclick = (e) => e.stopPropagation();
  
  menu.innerHTML = `
    <div class="menu-header">
      <h3>${esc(track.title)}</h3>
      <p>${esc(track.artist || '')}</p>
    </div>
    <div class="menu-items">
      <button class="menu-item" onclick="openInYouTube('${esc(currentId)}'); document.querySelector('.menu-overlay').remove()">
        <i class="fab fa-youtube"></i>
        <span>Открыть в YouTube</span>
      </button>
      <button class="menu-item" onclick="dlTrack('${esc(currentId)}'); document.querySelector('.menu-overlay').remove()">
        <i class="fas fa-download"></i>
        <span>Скачать трек</span>
      </button>
      <button class="menu-item" onclick="toggleFav('${esc(currentId)}'); document.querySelector('.menu-overlay').remove()">
        <i class="fas fa-heart"></i>
        <span>${favSet.has(currentId) ? 'Убрать из избранного' : 'Добавить в избранное'}</span>
      </button>
      <button class="menu-item" onclick="showLyrics('${esc(currentId)}','${esc(track.title)}','${esc(track.artist||'')}'); document.querySelector('.menu-overlay').remove()">
        <i class="fas fa-align-left"></i>
        <span>Показать текст</span>
      </button>
    </div>
    <button class="menu-close" onclick="document.querySelector('.menu-overlay').remove()">
      Закрыть
    </button>
  `;
  
  overlay.appendChild(menu);
  document.body.appendChild(overlay);
}
window.showPlayerMenu = showPlayerMenu;

function openInYouTube(trackId) {
  if (!trackId) return;
  window.open(`https://music.youtube.com/watch?v=${trackId}`, '_blank');
}
window.openInYouTube = openInYouTube;

// Click on mini-player opens fullscreen
$('mini-player') && $('mini-player').addEventListener('click', (e) => {
  // Don't open if clicked on play button
  if (e.target.closest('#mini-play') || e.target.closest('.mini-play')) return;
  openFullscreenPlayer();
});

// Click on desktop player info opens fullscreen
document.querySelector('.player-left') && document.querySelector('.player-left').addEventListener('click', () => {
  if (currentId) openFullscreenPlayer();
});

/* ══════════════════════════════════════════════
   MEDIA SESSION API (Lock Screen Controls)
══════════════════════════════════════════════ */
function setupMediaSession() {
  if (!('mediaSession' in navigator)) return;
  
  const audio = $('audio');
  if (!audio) return;
  
  // Set playback state
  navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
  
  // Listen for play/pause
  audio.addEventListener('play', () => {
    navigator.mediaSession.playbackState = 'playing';
    updateMediaSessionMetadata();
    updateFullscreenPlayButton();
  });
  
  audio.addEventListener('pause', () => {
    navigator.mediaSession.playbackState = 'paused';
    updateFullscreenPlayButton();
  });
  
  // Set action handlers
  navigator.mediaSession.setActionHandler('play', () => {
    audio.play();
  });
  
  navigator.mediaSession.setActionHandler('pause', () => {
    audio.pause();
  });
  
  navigator.mediaSession.setActionHandler('previoustrack', () => {
    prevTrack();
  });
  
  navigator.mediaSession.setActionHandler('nexttrack', () => {
    nextTrack();
  });
  
  navigator.mediaSession.setActionHandler('seekbackward', (details) => {
    const skipTime = details.seekOffset || 10;
    audio.currentTime = Math.max(audio.currentTime - skipTime, 0);
  });
  
  navigator.mediaSession.setActionHandler('seekforward', (details) => {
    const skipTime = details.seekOffset || 10;
    audio.currentTime = Math.min(audio.currentTime + skipTime, audio.duration || Infinity);
  });
  
  // Seek to position (if supported)
  try {
    navigator.mediaSession.setActionHandler('seekto', (details) => {
      if (details.seekTime !== undefined && !isNaN(details.seekTime)) {
        audio.currentTime = details.seekTime;
      }
    });
  } catch(e) {
    // seekto not supported on all platforms
  }
}

function updateMediaSessionMetadata() {
  if (!('mediaSession' in navigator) || !currentId || !playlist[playIdx]) return;
  
  const t = playlist[playIdx];
  
  // Create artwork array
  const artwork = [];
  if (t.cover) {
    artwork.push(
      { src: t.cover, sizes: '96x96', type: 'image/jpeg' },
      { src: t.cover, sizes: '128x128', type: 'image/jpeg' },
      { src: t.cover, sizes: '192x192', type: 'image/jpeg' },
      { src: t.cover, sizes: '256x256', type: 'image/jpeg' },
      { src: t.cover, sizes: '384x384', type: 'image/jpeg' },
      { src: t.cover, sizes: '512x512', type: 'image/jpeg' }
    );
  }
  
  navigator.mediaSession.metadata = new MediaMetadata({
    title: t.title || 'Unknown',
    artist: t.artist || 'Unknown Artist',
    album: t.album || '',
    artwork: artwork
  });
}

// Setup media session on init
window.addEventListener('load', setupMediaSession);

/* ══════════════════════════════════════════════
   HELPERS
══════════════════════════════════════════════ */
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function fmtSec(s) {
  if (!s || isNaN(s)) return '0:00';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
function fmtMs(ms) { return fmtSec((ms || 0) / 1000); }

/* ══════════════════════════════════════════════
   HOME PAGE — Charts & Sections
══════════════════════════════════════════════ */
async function loadHomeData() {
  const container = $('home-sections');
  if (!container) return;
  
  container.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  
  try {
    const r = await fetch(`${API}/api/home`, { credentials: 'include' });
    const d = await r.json();
    
    if (!d.sections || d.sections.length === 0) {
      container.innerHTML = '';
      return;
    }
    
    let html = '';
    d.sections.forEach((section, sIdx) => {
      html += `<div class="home-section">
        <div class="home-section-header">
          <i class="fas fa-${section.icon}"></i>
          <h2>${section.title}</h2>
        </div>
        <div class="home-scroll">`;
      
      section.tracks.forEach((t, tIdx) => {
        html += `<div class="home-card" onclick="playHomeTrack(${sIdx}, ${tIdx})">
          <div class="home-card-cover">
            <img src="${t.cover || ''}" alt="" loading="lazy">
          </div>
          <div class="home-card-title">${t.title}</div>
          <div class="home-card-artist">${t.artist}</div>
        </div>`;
      });
      
      html += '</div></div>';
    });
    
    container.innerHTML = html;
    
    // Store sections data for playback
    window._homeSections = d.sections;
  } catch (e) {
    console.error('Home data error:', e);
    container.innerHTML = '';
  }
}
window.loadHomeData = loadHomeData;

function playHomeTrack(sectionIdx, trackIdx) {
  if (!window._homeSections) return;
  const section = window._homeSections[sectionIdx];
  if (!section) return;
  
  // Set playlist to section tracks
  playlist = section.tracks;
  playIdx = trackIdx;
  loadAndPlay(playlist[playIdx]);
}
window.playHomeTrack = playHomeTrack;

/* ══════════════════════════════════════════════
   LYRICS OVERLAY
══════════════════════════════════════════════ */
function openLyrics() {
  const overlay = $('lyrics-overlay');
  if (!overlay) return;
  
  const t = playlist[playIdx];
  if (!t) return;
  
  // Set track info
  const titleEl = $('lyrics-track-title');
  const artistEl = $('lyrics-track-artist');
  if (titleEl) titleEl.textContent = t.title || '';
  if (artistEl) artistEl.textContent = t.artist || '';
  
  overlay.classList.add('active');
  
  // Load lyrics
  const body = $('lyrics-body');
  if (body) body.innerHTML = '<div class="loading-state"><div class="spinner"></div></div>';
  
  fetch(`${API}/api/track/${t.id}/lyrics`, { credentials: 'include' })
    .then(r => r.json())
    .then(d => {
      if (d.lyrics) {
        const lines = d.lyrics.split('\n');
        body.innerHTML = lines.map(l => `<div class="lyrics-line">${l || '&nbsp;'}</div>`).join('');
      } else {
        body.innerHTML = '<div style="text-align:center;color:#888;padding:40px"><i class="fas fa-music" style="font-size:48px;margin-bottom:16px;display:block"></i>Текст не найден</div>';
      }
    })
    .catch(() => {
      body.innerHTML = '<div style="text-align:center;color:#888;padding:40px">Ошибка загрузки</div>';
    });
}
window.openLyrics = openLyrics;

function closeLyrics() {
  const overlay = $('lyrics-overlay');
  if (overlay) overlay.classList.remove('active');
}
window.closeLyrics = closeLyrics;

/* ══════════════════════════════════════════════
   QUEUE
══════════════════════════════════════════════ */
function openQueue() {
  const overlay = $('queue-overlay');
  if (!overlay) {
    // Create queue overlay dynamically
    const div = document.createElement('div');
    div.id = 'queue-overlay';
    div.className = 'queue-overlay';
    div.innerHTML = `
      <div class="queue-header">
        <h3><i class="fas fa-list"></i> Очередь</h3>
        <button class="queue-close" onclick="closeQueue()"><i class="fas fa-times"></i></button>
      </div>
      <div class="queue-list" id="queue-list"></div>
    `;
    document.body.appendChild(div);
  }
  
  updateQueueList();
  setTimeout(() => $('queue-overlay').classList.add('active'), 10);
}
window.openQueue = openQueue;

function closeQueue() {
  const overlay = $('queue-overlay');
  if (overlay) overlay.classList.remove('active');
}
window.closeQueue = closeQueue;

function updateQueueList() {
  const list = $('queue-list');
  if (!list) return;
  
  if (playlist.length === 0) {
    list.innerHTML = '<div style="text-align:center;color:#888;padding:40px">Очередь пуста</div>';
    return;
  }
  
  list.innerHTML = playlist.map((t, i) => `
    <div class="queue-item ${i === playIdx ? 'active' : ''}" onclick="playFromList(${i});closeQueue()">
      <img class="queue-item-cover" src="${t.cover || ''}" alt="">
      <div class="queue-item-info">
        <div class="queue-item-title">${i === playIdx ? '<i class="fas fa-volume-up" style="color:var(--accent);margin-right:6px"></i>' : ''}${t.title}</div>
        <div class="queue-item-artist">${t.artist || ''}</div>
      </div>
    </div>
  `).join('');
}

/* ══════════════════════════════════════════════
   OFFLINE CACHE (IndexedDB)
══════════════════════════════════════════════ */
const OFFLINE_DB_NAME = 'zvonko_offline';
const OFFLINE_DB_VER = 1;
let offlineDB = null;

function openOfflineDB() {
  return new Promise((resolve, reject) => {
    if (offlineDB) { resolve(offlineDB); return; }
    const req = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VER);
    req.onupgradeneeded = (e) => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('tracks')) {
        db.createObjectStore('tracks', { keyPath: 'id' });
      }
    };
    req.onsuccess = (e) => { offlineDB = e.target.result; resolve(offlineDB); };
    req.onerror = () => reject(req.error);
  });
}

async function saveTrackOffline(trackId) {
  try {
    showToast('Сохранение для офлайн...', 'info');
    
    // Download audio
    const audioResp = await fetch(`${API}/api/track/${trackId}/stream`);
    const audioBlob = await audioResp.blob();
    
    // Get track info
    const t = playlist.find(t => t.id === trackId) || { id: trackId, title: '', artist: '', cover: '' };
    
    const db = await openOfflineDB();
    const tx = db.transaction('tracks', 'readwrite');
    const store = tx.objectStore('tracks');
    
    store.put({
      id: trackId,
      title: t.title,
      artist: t.artist,
      cover: t.cover,
      audio: audioBlob,
      savedAt: Date.now(),
    });
    
    await new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = () => reject(tx.error);
    });
    
    showToast('Сохранено для офлайн!');
  } catch (e) {
    console.error('Offline save error:', e);
    showToast('Ошибка сохранения', 'error');
  }
}
window.saveTrackOffline = saveTrackOffline;

async function getOfflineTrack(trackId) {
  try {
    const db = await openOfflineDB();
    const tx = db.transaction('tracks', 'readonly');
    const store = tx.objectStore('tracks');
    const req = store.get(trackId);
    return new Promise((resolve) => {
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => resolve(null);
    });
  } catch { return null; }
}

async function getOfflineTracks() {
  try {
    const db = await openOfflineDB();
    const tx = db.transaction('tracks', 'readonly');
    const store = tx.objectStore('tracks');
    const req = store.getAll();
    return new Promise((resolve) => {
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => resolve([]);
    });
  } catch { return []; }
}

async function deleteOfflineTrack(trackId) {
  try {
    const db = await openOfflineDB();
    const tx = db.transaction('tracks', 'readwrite');
    tx.objectStore('tracks').delete(trackId);
    showToast('Удалено из офлайн');
    loadOfflineTracks();
  } catch { }
}
window.deleteOfflineTrack = deleteOfflineTrack;

async function loadOfflineTracks() {
  const container = $('offline-list');
  if (!container) return;
  
  try {
    const tracks = await getOfflineTracks();
    
    if (tracks.length === 0) {
      container.innerHTML = `
        <div style="text-align:center;color:#888;padding:60px 20px">
          <i class="fas fa-download" style="font-size:48px;margin-bottom:16px;display:block"></i>
          <p>Нет сохранённых треков</p>
          <p style="font-size:13px;margin-top:8px">Нажмите "Офлайн" в полноэкранном плеере, чтобы сохранить трек</p>
        </div>
      `;
      return;
    }
    
    container.innerHTML = tracks.map((t, i) => {
      const cover = t.cover ? `<img src="${t.cover}" alt="" loading="lazy">` : '<i class="fas fa-music"></i>';
      return `
      <div class="track-item" data-id="${t.id}" onclick="playOfflineTrack('${t.id}')">
        <div class="t-num">
          <span class="num-txt">${i + 1}</span>
        </div>
        <div class="t-cover">${cover}</div>
        <div class="t-info">
          <div class="t-title">${t.title}</div>
          <div class="t-artist">${t.artist || ''}</div>
        </div>
        <div class="t-dur">
          <i class="fas fa-check-circle" style="color:var(--accent)"></i>
        </div>
        <div class="t-actions">
          <button class="t-btn" title="Удалить" onclick="event.stopPropagation();deleteOfflineTrack('${t.id}')">
            <i class="fas fa-trash"></i>
          </button>
        </div>
      </div>
    `;
    }).join('');
  } catch(e) {
    console.error('loadOfflineTracks error:', e);
    container.innerHTML = '<div class="empty-state"><p>Ошибка загрузки</p></div>';
  }
}
window.loadOfflineTracks = loadOfflineTracks;

async function playOfflineTrack(trackId) {
  const offlineData = await getOfflineTrack(trackId);
  if (!offlineData || !offlineData.audio) {
    showToast('Трек не найден', 'error');
    return;
  }
  
  const blobUrl = URL.createObjectURL(offlineData.audio);
  const t = {
    id: offlineData.id,
    title: offlineData.title,
    artist: offlineData.artist,
    cover: offlineData.cover,
  };
  
  playlist = [t];
  playIdx = 0;
  await loadAudioSource(blobUrl);
  setPlaying(true);
}
window.playOfflineTrack = playOfflineTrack;

/* ══════════════════════════════════════════════
   INIT
══════════════════════════════════════════════ */
checkAuth();
loadRecent();
loadFavorites();
loadHomeData();
