(function () {
  var d = document.getElementById("course-contents");
  if (!d) return;
  var toc = d.querySelector("nav.toc");
  var here = location.pathname.split("/").pop() || "index.html";

  // The decisions of each module, in page order. Their anchors follow dN-k and their numbers N.k.
  // Keep this list in step with the <h4 class="q"> headings of module-N-rev.html.
  var DECISIONS = {
    1: ["How many AWS accounts?", "Public or private network?", "Who creates infrastructure, and how?", "Who builds and runs the agents?"],
    2: ["Who signs people in?", "How do agents authenticate to the tool gateway?", "Where do agent secrets live?", "Which login do employees use, and where does it run?", "How does the employee's identity reach the tools?"],
    3: ["How are tenants modelled?", "Governance: which rule lives at which level?", "What needs approval, and who approves?", "The catalogue: who may find it, who may change it, and how many registries?"],
    4: ["Gateway layout: how many gateways, and whose?", "Single layer or multi-layer: tools only, or agents behind gateways too?", "How do tools and knowledge get in, and how is access bounded?", "How are tool policies rolled out?", "How does a tool receive the employee's token?"],
    5: ["How do agents reach models?", "How is spend controlled?", "Where does the catalogue live?", "Where may a model request be processed?"],
    6: ["What is an agent, and what is the unit of release?", "How does a release pass?", "How does it reach Prod?"],
    7: ["What do we watch, and from where?", "How is quality watched in Prod?", "What may telemetry keep, and who may read it?", "How is shared cost allocated?", "Recap: what changes for production"]
  };
  // The sections every module page has, in page order: section id and menu label.
  var SECTIONS = [["explain", "Explain"], ["decisions", "Decisions"], ["hands-on", "Hands-on"], ["architecture", "Architecture"], ["glossary", "Glossary"]];

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text) e.textContent = text;
    return e;
  }
  function link(href, cls, text) {
    var a = el("a", cls, text);
    a.setAttribute("href", href);
    return a;
  }
  function setOpen(btn, sub, open) {
    sub.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // Module rows get a chevron on the right that shows the module's sections and decisions underneath.
  // It points left while collapsed and down while expanded. Every module starts collapsed; the row of the page you are reading is highlighted.
  toc.querySelectorAll("a").forEach(function (a) {
    var page = a.getAttribute("href");
    var m = /^module-(\d)-rev\.html$/.exec(page);
    if (!m) return;
    var n = m[1];
    var id = "toc-sub-" + n;
    var row = el("div", "toc-row");
    var btn = el("button", "toc-more");
    btn.type = "button";
    btn.setAttribute("aria-controls", id);
    btn.setAttribute("aria-label", "Sections of module " + n);
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 6-6 6 6 6" /></svg>';
    var sub = el("div", "toc-sub");
    sub.id = id;
    sub.setAttribute("role", "group");
    sub.setAttribute("aria-label", "Module " + n + " sections");
    SECTIONS.forEach(function (s) {
      sub.appendChild(link(page + "#" + s[0], "", s[1]));
      if (s[0] !== "decisions") return;
      (DECISIONS[n] || []).forEach(function (title, i) {
        var dec = link(page + "#d" + n + "-" + (i + 1), "dec");
        dec.appendChild(el("span", "n", n + "." + (i + 1)));
        dec.appendChild(el("span", "t", title));
        sub.appendChild(dec);
      });
    });
    btn.addEventListener("click", function () {
      setOpen(btn, sub, sub.hidden);
    });
    a.parentNode.insertBefore(row, a);
    row.appendChild(a);
    row.appendChild(btn);
    row.parentNode.insertBefore(sub, row.nextSibling);
    setOpen(btn, sub, false);
  });

  toc.querySelectorAll("a").forEach(function (a) {
    a.addEventListener("click", function () {
      d.open = false;
    });
    if (a.getAttribute("href") === here) a.classList.add("here");
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") d.open = false;
  });
  document.addEventListener("click", function (e) {
    if (d.open && !d.contains(e.target)) d.open = false;
  });

  // Back to top: hidden while the top of the page is in view (the markup ships it hidden, so nothing fades at load),
  // shown once the reader has scrolled past the hero.
  var top = document.querySelector(".to-top");
  if (!top) return;
  var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function place() {
    top.classList.toggle("at-top", window.scrollY < 240);
  }
  place();
  window.addEventListener("scroll", place, { passive: true });
  top.addEventListener("click", function (e) {
    e.preventDefault();
    window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
  });
})();
