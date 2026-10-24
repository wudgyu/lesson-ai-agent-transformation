/* ============================================================
   AI Agent 转型课 — shared interactions
   - quiz with immediate feedback
   - copy buttons for code blocks
   - reading progress bar
   - checklist persistence (optional, via data-persist)
   No dependencies. Works from file://.
   ============================================================ */

(function () {
  "use strict";

  function initQuizzes() {
    document.querySelectorAll("[data-quiz]").forEach(function (quiz) {
      var questions = Array.prototype.slice.call(quiz.querySelectorAll(".quiz-q"));
      var scoreEl = quiz.querySelector(".quiz-score");
      var answered = 0;
      var correct = 0;

      function renderScore() {
        if (!scoreEl) return;
        if (answered === 0) {
          scoreEl.textContent = "";
          return;
        }
        var tail = answered === questions.length
          ? (correct === questions.length ? " · 全对，可以进入 lab 了。" : " · 回到错题，说明你的判断依据。")
          : "";
        scoreEl.textContent = "已答 " + answered + " / " + questions.length +
          " · 正确 " + correct + tail;
      }

      questions.forEach(function (q) {
        var answer = (q.getAttribute("data-answer") || "").toLowerCase();
        var explain = q.getAttribute("data-explain") || "";
        var options = Array.prototype.slice.call(q.querySelectorAll(".quiz-opt"));
        var feedback = q.querySelector(".quiz-feedback");
        var done = false;

        options.forEach(function (opt) {
          opt.addEventListener("click", function () {
            if (done) return;
            done = true;
            var picked = (opt.getAttribute("data-opt") || "").toLowerCase();
            var isRight = picked === answer;

            options.forEach(function (o) {
              o.disabled = true;
              var value = (o.getAttribute("data-opt") || "").toLowerCase();
              if (value === answer) o.classList.add("is-correct");
              else if (o === opt) o.classList.add("is-wrong");
              else o.classList.add("is-dimmed");
            });

            if (feedback) {
              feedback.hidden = false;
              feedback.classList.toggle("is-wrong", !isRight);
              var head = isRight
                ? "<strong>正确。</strong> "
                : "<strong>不对。</strong>正确答案是 " + answer.toUpperCase() + "。 ";
              feedback.innerHTML = head + explain;
            }

            answered += 1;
            if (isRight) correct += 1;
            renderScore();
          });
        });
      });

      renderScore();
    });
  }

  function copyText(text, onDone) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(onDone, function () {
        fallbackCopy(text, onDone);
      });
    } else {
      fallbackCopy(text, onDone);
    }
  }

  function fallbackCopy(text, onDone) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
    onDone();
  }

  function initCopyButtons() {
    document.querySelectorAll("pre").forEach(function (pre) {
      if (pre.parentElement && pre.parentElement.classList.contains("code-wrap")) return;
      var wrap = document.createElement("div");
      wrap.className = "code-wrap";
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);

      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy-btn";
      btn.textContent = "复制";
      btn.setAttribute("aria-label", "复制代码");
      btn.addEventListener("click", function () {
        copyText(pre.innerText, function () {
          btn.textContent = "已复制";
          btn.classList.add("copied");
          window.setTimeout(function () {
            btn.textContent = "复制";
            btn.classList.remove("copied");
          }, 1600);
        });
      });
      wrap.appendChild(btn);
    });
  }

  function initProgress() {
    var bar = document.querySelector(".progress-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "progress-bar";
      bar.setAttribute("aria-hidden", "true");
      document.body.appendChild(bar);
    }
    function update() {
      var doc = document.documentElement;
      var max = doc.scrollHeight - window.innerHeight;
      var ratio = max > 0 ? Math.min(1, Math.max(0, window.scrollY / max)) : 0;
      bar.style.width = (ratio * 100).toFixed(2) + "%";
    }
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
  }

  function initChecklists() {
    document.querySelectorAll(".checklist[data-persist]").forEach(function (list) {
      var key = "teach:" + location.pathname + ":" + (list.getAttribute("data-persist") || "checklist");
      var boxes = Array.prototype.slice.call(list.querySelectorAll("input[type=checkbox]"));
      var saved = {};
      try { saved = JSON.parse(localStorage.getItem(key) || "{}"); } catch (e) { saved = {}; }
      boxes.forEach(function (box, i) {
        if (saved[i]) box.checked = true;
        box.addEventListener("change", function () {
          var state = {};
          boxes.forEach(function (b, j) { state[j] = b.checked; });
          try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) { /* ignore */ }
        });
      });
    });
  }

  function init() {
    initQuizzes();
    initCopyButtons();
    initProgress();
    initChecklists();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
