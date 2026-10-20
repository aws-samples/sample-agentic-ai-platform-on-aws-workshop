(function () {
  var d = document.getElementById("course-contents");
  if (!d) return;
  d.querySelectorAll("nav.toc a").forEach(function (a) {
    a.addEventListener("click", function () {
      d.open = false;
    });
    var here = location.pathname.split("/").pop() || "index.html";
    if (a.getAttribute("href") === here) a.classList.add("here");
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") d.open = false;
  });
  document.addEventListener("click", function (e) {
    if (d.open && !d.contains(e.target)) d.open = false;
  });
})();
