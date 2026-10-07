// Small helpers for the admin UI: sortable columns and "N visible rows" lists.
(function () {
  "use strict";

  // "05.10.2026 10:02:39" -> sortable number; "01:02:05" -> seconds; "1 234" / "39" -> number
  function key(cell) {
    var raw = cell.getAttribute("data-sort");
    var t = (raw !== null ? raw : cell.textContent).trim();
    var m = t.match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?/);
    if (m) return { n: Number(m[3] + m[2] + m[1] + m[4] + m[5] + (m[6] || "00")) };
    m = t.match(/^(\d+):(\d{2}):(\d{2})$/);
    if (m) return { n: Number(m[1]) * 3600 + Number(m[2]) * 60 + Number(m[3]) };
    m = t.match(/^#?(-?[\d\s ]+(?:[.,]\d+)?)(?:\s|$)/);
    if (m) return { n: Number(m[1].replace(/[\s ]/g, "").replace(",", ".")) };
    if (t === "—" || t === "") return { n: null, s: "" };
    return { s: t.toLowerCase() };
  }

  function isEmpty(k) { return k.n === null; }

  function compare(a, b) {
    if (a.n !== undefined && b.n !== undefined) return a.n - b.n;
    return (a.s || String(a.n)).localeCompare(b.s || String(b.n), "ru");
  }

  function makeSortable(table) {
    var head = table.querySelector("tr");
    if (!head || !head.querySelector("th")) return;
    Array.prototype.forEach.call(head.children, function (th, idx) {
      if (th.classList.contains("nosort") || !th.textContent.trim()) {
        th.classList.add("nosort");
        return;
      }
      th.title = "Сортировать";
      th.addEventListener("click", function () {
        var dir = th.getAttribute("data-dir") === "asc" ? "desc" : "asc";
        Array.prototype.forEach.call(head.children, function (o) { o.removeAttribute("data-dir"); });
        th.setAttribute("data-dir", dir);
        var body = head.parentNode;
        var rows = Array.prototype.filter.call(body.children, function (r) {
          return r !== head && r.children.length > 1; // skip the header and "no data" rows
        });
        rows.sort(function (r1, r2) {
          var k1 = key(r1.children[idx]), k2 = key(r2.children[idx]);
          if (isEmpty(k1) || isEmpty(k2)) return isEmpty(k1) - isEmpty(k2); // "—" always at the bottom
          var c = compare(k1, k2);
          return dir === "asc" ? c : -c;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  }

  // a list shows exactly its header + first N rows; the rest scrolls (rows have different heights)
  function fitRows(wrap) {
    var n = Number(wrap.getAttribute("data-rows") || 5);
    var rows = wrap.querySelectorAll("tr");
    if (rows.length <= n + 1) { wrap.style.maxHeight = "none"; return; }
    var h = 0;
    for (var i = 0; i <= n; i++) h += rows[i].getBoundingClientRect().height;
    wrap.style.maxHeight = Math.ceil(h + 2) + "px";
  }

  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("table.sortable"), makeSortable);
    var lists = document.querySelectorAll(".table-wrap.rows5");
    Array.prototype.forEach.call(lists, fitRows);
    window.addEventListener("resize", function () { Array.prototype.forEach.call(lists, fitRows); });
  });
})();
