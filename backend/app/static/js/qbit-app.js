/* ============================================================================
   QBIT CONNECT — application shell JS (design system v2)
   - Command palette (Ctrl+K): page jump index + real API search
   - Sidebar collapse (persisted)
   No frameworks. No fake data: empty/error results render honestly.
   ========================================================================== */
(function () {
  "use strict";

  var shell = document.getElementById("qbit-shell");
  if (!shell) return;

  /* ------------------------------------------------- sidebar collapse */
  try {
    if (localStorage.getItem("qbit.nav-collapsed") === "1") {
      shell.classList.add("nav-collapsed");
    }
  } catch (e) { /* storage unavailable */ }
  var toggle = document.getElementById("nav-toggle");
  if (toggle) toggle.addEventListener("click", function () {
    var collapsed = shell.classList.toggle("nav-collapsed");
    try { localStorage.setItem("qbit.nav-collapsed", collapsed ? "1" : "0"); } catch (e) {}
  });

  /* mobile drawer */
  var sidebar = document.getElementById("qbit-sidebar");
  var mobileBtn = document.getElementById("mobile-menu");
  if (mobileBtn && sidebar) {
    mobileBtn.addEventListener("click", function () { sidebar.classList.toggle("open"); });
    document.addEventListener("click", function (ev) {
      if (sidebar.classList.contains("open") &&
          !sidebar.contains(ev.target) && ev.target !== mobileBtn &&
          !mobileBtn.contains(ev.target)) sidebar.classList.remove("open");
    });
  }

  /* ------------------------------------------------- command palette */
  var backdrop = document.getElementById("palette-backdrop");
  var input = document.getElementById("palette-input");
  var list = document.getElementById("palette-list");
  var trigger = document.getElementById("palette-trigger");
  if (!backdrop || !input || !list) return;

  var ICON = '<svg fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><use href="#i-ico"/></svg>';

  /* Static page index — navigation only, no invented destinations.
     Every entry carries its group; headers are derived at render time. */
  var NAV_INDEX = [
    { label: "Home", href: "/", ico: "i-home", group: "Workspace" },
    { label: "Leads", href: "/leads", ico: "i-leads", group: "Workspace" },
    { label: "Imports", href: "/leads/import", ico: "i-import", group: "Workspace" },
    { label: "Exports", href: "/leads/exports", ico: "i-export", group: "Workspace" },
    { label: "Scrapers", href: "/scraping", ico: "i-scraper", group: "Workspace" },
    { label: "Jobs", href: "/scraping/jobs", ico: "i-jobs", group: "Workspace" },
    { label: "Results", href: "/scraping/jobs?status=COMPLETED", ico: "i-results", group: "Workspace" },
    { label: "Campaigns", href: "/campaigns", ico: "i-campaigns", group: "Growth" },
    { label: "Connections", href: "/connections", ico: "i-connections", group: "Growth" },
    { label: "Inbox", href: "/inbox", ico: "i-inbox", group: "Growth" },
    { label: "Automation", href: "/automation", ico: "i-automation", group: "Growth" },
    { label: "Analytics", href: "/analytics", ico: "i-analytics", group: "Intelligence" },
    { label: "Admin Overview", href: "/admin", ico: "i-admin", group: "Admin" },
    { label: "Users", href: "/admin/users", ico: "i-users", group: "Admin" },
    { label: "Teams", href: "/admin/teams", ico: "i-teams", group: "Admin" },
    { label: "Roles", href: "/admin/roles", ico: "i-roles", group: "Admin" },
    { label: "Invitations", href: "/admin/invitations", ico: "i-invitations", group: "Admin" },
    { label: "Access", href: "/admin/connections", ico: "i-access", group: "Admin" },
    { label: "API Keys", href: "/admin/api-keys", ico: "i-keys", group: "Admin" },
    { label: "Security", href: "/admin/security", ico: "i-security", group: "Admin" },
    { label: "Audit Logs", href: "/admin/audit", ico: "i-audit", group: "Admin" },
    { label: "Org Settings", href: "/admin/settings", ico: "i-settings", group: "Admin" }
  ];

  var RECENTS_KEY = "qbit.palette-recents";
  function recents() {
    try { return JSON.parse(localStorage.getItem(RECENTS_KEY) || "[]"); } catch (e) { return []; }
  }
  function pushRecent(item) {
    if (!item || !item.href) return;
    var rs = recents().filter(function (r) { return r.href !== item.href; });
    rs.unshift({ label: item.label, href: item.href, ico: item.ico || "i-chevron", group: item.group });
    try { localStorage.setItem(RECENTS_KEY, JSON.stringify(rs.slice(0, 6))); } catch (e) {}
  }

  var items = [];       /* flattened renderable items */
  var selected = 0;
  var searchSeq = 0;

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function icon(ico) { return ICON.replace("#i-ico", ico); }

  /* Live search against EXISTING APIs (session-cookie auth).
     - leads: server-side search (`search=` param)
     - jobs / campaigns: latest page fetched, filtered client-side (honest v1) */
  function fetchJSON(url) {
    return fetch(url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  function rows(payload, dataKey) {
    var d = payload && (payload.data || payload);
    if (!d) return [];
    if (Array.isArray(d)) return d;
    return d.items || d[dataKey] || [];
  }

  function searchAPI(q) {
    var seq = ++searchSeq;
    var jobs = [
      fetchJSON("/api/v1/leads?search=" + encodeURIComponent(q) + "&page_size=6")
        .then(function (p) {
          return rows(p).map(function (l) {
            return { label: l.business_name || l.contact_name || l.email || "(lead)", href: "/leads/" + (l.id || ""), ico: "i-leads", group: "Leads", hint: l.city || l.company || "" };
          });
        }),
      fetchJSON("/api/v1/scrape-jobs?page_size=25").then(function (p) {
        var ql = q.toLowerCase();
        return rows(p).filter(function (j) {
          return String(j.actor_id || "").toLowerCase().indexOf(ql) >= 0 ||
                 String(j.status || "").toLowerCase().indexOf(ql) >= 0 ||
                 String(j.id || "").toLowerCase().indexOf(ql) >= 0;
        }).slice(0, 5).map(function (j) {
          return { label: "Job " + String(j.id || "").slice(0, 8) + " — " + (j.actor_id || ""), href: "/scraping/jobs/" + (j.id || ""), ico: "i-jobs", group: "Jobs", hint: j.status || "" };
        });
      }),
      fetchJSON("/api/v1/campaigns?page_size=25").then(function (p) {
        var ql = q.toLowerCase();
        return rows(p).filter(function (c) {
          return String(c.name || "").toLowerCase().indexOf(ql) >= 0;
        }).slice(0, 5).map(function (c) {
          return { label: c.name || "(campaign)", href: "/campaigns/" + (c.id || ""), ico: "i-campaigns", group: "Campaigns", hint: c.status || c.channel || "" };
        });
      })
    ];
    return Promise.all(jobs).then(function (rs) {
      if (seq !== searchSeq) return null; /* stale response */
      return rs.filter(function (a) { return a && a.length; })[0] ||
             rs.reduce(function (acc, a) { return acc.concat(a || []); }, []);
    });
  }

  function render(html) { list.innerHTML = html; }
  function empty(msg) { return '<div class="palette-empty">' + esc(msg) + "</div>"; }

  function groupRender(list_) {
    var selectable = list_.filter(function (it) { return it && it.href && it.label; });
    if (!selectable.length) return empty("No matches.");
    var out = [], lastGroup = null;
    selectable.forEach(function (it, idx) {
      if (it.group !== lastGroup) {
        out.push('<div class="palette-group">' + esc(it.group) + "</div>");
        lastGroup = it.group;
      }
      out.push(
        '<div class="palette-item' + (idx === selected ? " selected" : "") + '" data-href="' + esc(it.href) + '" data-idx="' + idx + '" role="option" aria-selected="' + (idx === selected) + '">' +
        icon(it.ico) + "<span>" + esc(it.label) + "</span>" +
        (it.hint ? '<span class="pi-hint">' + esc(it.hint) + "</span>" : "") +
        "</div>"
      );
    });
    return out.join("");
  }

  function setItems(list_) {
    items = list_.filter(function (it) { return it && it.href && it.label; });
    selected = 0;
    render(groupRender(items));
  }

  function go(it) {
    if (!it || !it.href) return;
    pushRecent(it);
    location.href = it.href;
  }

  function doSearch(q) {
    if (!q) {
      var rs = recents();
      setItems(rs.length ? rs : NAV_INDEX);
      return;
    }
    var ql = q.toLowerCase();
    var navMatches = NAV_INDEX.filter(function (it) {
      return it.label && it.label.toLowerCase().indexOf(ql) >= 0;
    });
    setItems(navMatches);
    render(groupRender(items) || empty("Searching…"));
    searchAPI(q).then(function (found) {
      if (found === null) return;                 /* stale */
      var merged = navMatches.concat(found.filter(function (f) {
        return !navMatches.some(function (n) { return n.href === f.href; });
      }));
      setItems(merged);
      if (!items.length) render(empty("No matches. Try a lead name, job id or page name."));
    });
  }

  var debounce = null;
  input.addEventListener("input", function () {
    clearTimeout(debounce);
    debounce = setTimeout(function () { doSearch(input.value.trim()); }, 200);
  });

  input.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      ev.preventDefault();
      if (!items.length) return;
      selected = (selected + (ev.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      render(groupRender(items));
      var el = list.querySelector('[data-idx="' + selected + '"]');
      if (el) el.scrollIntoView({ block: "nearest" });
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      go(items[selected]);
    }
  });

  list.addEventListener("click", function (ev) {
    var el = ev.target.closest(".palette-item");
    if (el) go(items[Number(el.dataset.idx)]);
  });
  list.addEventListener("mousemove", function (ev) {
    var el = ev.target.closest(".palette-item");
    if (el && Number(el.dataset.idx) !== selected) {
      selected = Number(el.dataset.idx);
      render(groupRender(items));
    }
  });

  function open() {
    backdrop.classList.add("open");
    input.value = "";
    doSearch("");
    setTimeout(function () { input.focus(); }, 30);
  }
  function close() { backdrop.classList.remove("open"); }

  if (trigger) trigger.addEventListener("click", open);
  backdrop.addEventListener("mousedown", function (ev) { if (ev.target === backdrop) close(); });
  document.addEventListener("keydown", function (ev) {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) {
      ev.preventDefault();
      backdrop.classList.contains("open") ? close() : open();
    } else if (ev.key === "Escape" && backdrop.classList.contains("open")) {
      close();
    }
  });
})();
