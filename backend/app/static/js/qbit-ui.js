/* ============================================================
   QBIT CONNECT — Interactive UI Components (qbit-ui.js)
   Command palette (Ctrl+K), sidebar toggle, tooltips, toasts
   ============================================================ */

(function () {
  'use strict';

  // 1. Sidebar Collapse State (localStorage)
  const sidebar = document.querySelector('.sidebar');
  const collapseBtn = document.getElementById('sidebar-collapse-btn');
  if (sidebar && collapseBtn) {
    const isCollapsed = localStorage.getItem('qbit_sidebar_collapsed') === 'true';
    if (isCollapsed) {
      sidebar.classList.add('collapsed');
    }
    collapseBtn.addEventListener('click', function () {
      sidebar.classList.toggle('collapsed');
      localStorage.setItem('qbit_sidebar_collapsed', sidebar.classList.contains('collapsed'));
    });
  }

  // 2. Command Palette (Ctrl+K or Cmd+K)
  const paletteOverlay = document.getElementById('command-palette-overlay');
  const paletteInput = document.getElementById('palette-input');
  const paletteResults = document.getElementById('palette-results');
  const searchTriggers = document.querySelectorAll('.search-trigger, [data-trigger-palette]');

  const commandItems = [
    { name: 'Scrapers Directory', category: 'Workspace', url: '/scraping', icon: '⚙' },
    { name: 'Scraper Jobs', category: 'Workspace', url: '/scraping/jobs', icon: '⏱' },
    { name: 'Scraper Results', category: 'Workspace', url: '/scraping/jobs?status=COMPLETED', icon: '📊' },
    { name: 'Leads Workspace', category: 'Workspace', url: '/leads', icon: '◆' },
    { name: 'Import Leads', category: 'Workspace', url: '/leads/import', icon: '⇡' },
    { name: 'Export Data', category: 'Workspace', url: '/leads/exports', icon: '⇣' },
    { name: 'Campaigns', category: 'Growth', url: '/campaigns', icon: '✉' },
    { name: 'Connections', category: 'Growth', url: '/connections', icon: '▪' },
    { name: 'Unified Inbox', category: 'Growth', url: '/inbox', icon: '✈' },
    { name: 'Automation Workflows', category: 'Growth', url: '/automation', icon: '⚡' },
    { name: 'Analytics & KPIs', category: 'Intelligence', url: '/analytics', icon: '📈' },
    { name: 'Admin Overview', category: 'Admin', url: '/admin', icon: '⚐' },
    { name: 'Team Members', category: 'Admin', url: '/admin/users', icon: '👤' },
    { name: 'Roles & Permissions', category: 'Admin', url: '/admin/roles', icon: '🛡' },
    { name: 'Security Center', category: 'Admin', url: '/admin/security', icon: '🔒' },
    { name: 'Audit Logs', category: 'Admin', url: '/admin/audit', icon: '📝' },
    { name: 'Organization Settings', category: 'Admin', url: '/admin/settings', icon: '⚙' }
  ];

  let selectedIndex = 0;
  let filteredItems = [...commandItems];

  function renderPalette(items) {
    if (!paletteResults) return;
    if (items.length === 0) {
      paletteResults.innerHTML = '<div class="palette-empty">No matching commands or navigation items</div>';
      return;
    }

    // Group by category
    const groups = {};
    items.forEach((item, idx) => {
      if (!groups[item.category]) groups[item.category] = [];
      groups[item.category].push({ item, originalIdx: idx });
    });

    let html = '';
    let runningIdx = 0;
    for (const [category, list] of Object.entries(groups)) {
      html += `<div class="palette-group-label">${category}</div>`;
      list.forEach(({ item }) => {
        const isFocused = runningIdx === selectedIndex ? 'focused' : '';
        html += `
          <a href="${item.url}" class="palette-item ${isFocused}" data-index="${runningIdx}">
            <span class="palette-item-ico">${item.icon}</span>
            <span class="palette-item-name">${item.name}</span>
            <span class="palette-item-category">${item.category}</span>
          </a>
        `;
        runningIdx++;
      });
    }
    paletteResults.innerHTML = html;
  }

  function openPalette() {
    if (!paletteOverlay) return;
    paletteOverlay.classList.add('open');
    if (paletteInput) {
      paletteInput.value = '';
      filteredItems = [...commandItems];
      selectedIndex = 0;
      renderPalette(filteredItems);
      paletteInput.focus();
    }
  }

  function closePalette() {
    if (!paletteOverlay) return;
    paletteOverlay.classList.remove('open');
  }

  if (paletteOverlay) {
    paletteOverlay.addEventListener('click', function (e) {
      if (e.target === paletteOverlay) closePalette();
    });
  }

  searchTriggers.forEach(trigger => {
    trigger.addEventListener('click', function (e) {
      e.preventDefault();
      openPalette();
    });
  });

  document.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if (paletteOverlay && paletteOverlay.classList.contains('open')) {
        closePalette();
      } else {
        openPalette();
      }
    } else if (e.key === 'Escape' && paletteOverlay && paletteOverlay.classList.contains('open')) {
      closePalette();
    }
  });

  if (paletteInput) {
    paletteInput.addEventListener('input', function () {
      const q = paletteInput.value.toLowerCase().trim();
      filteredItems = commandItems.filter(it => 
        it.name.toLowerCase().includes(q) || it.category.toLowerCase().includes(q)
      );
      selectedIndex = 0;
      renderPalette(filteredItems);
    });

    paletteInput.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        selectedIndex = (selectedIndex + 1) % filteredItems.length;
        renderPalette(filteredItems);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        selectedIndex = (selectedIndex - 1 + filteredItems.length) % filteredItems.length;
        renderPalette(filteredItems);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (filteredItems[selectedIndex]) {
          window.location.href = filteredItems[selectedIndex].url;
        }
      }
    });
  }

  // 3. Simple Toast System
  window.qbitToast = function (message, type = 'info') {
    let container = document.getElementById('qbit-toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'qbit-toast-container';
      container.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
      document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = `alert alert-${type}`;
    toast.style.cssText = 'box-shadow:0 8px 24px rgba(0,0,0,0.5);min-width:240px;animation:palette-in 0.2s ease;';
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transition = 'opacity 0.3s ease';
      setTimeout(() => toast.remove(), 300);
    }, 4000);
  };

})();
