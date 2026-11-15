/* app.js —— 表单请求与错误提示；状态集中在一个对象里。 */
(function () {
  "use strict";

  var state = { busy: false };

  function message(text, isError) {
    var node = document.getElementById("import-msg");
    if (!node) {
      return;
    }
    node.textContent = text;
    node.className = isError ? "msg err" : "msg";
  }

  function pickInput() {
    var file = document.getElementById("file-input");
    var sample = document.getElementById("sample-select");
    var form = new FormData();
    if (file && file.files && file.files.length) {
      form.append("file", file.files[0]);
    } else if (sample) {
      form.append("sample", sample.value);
    }
    return form;
  }

  function send(url, form, done) {
    if (state.busy) {
      return;
    }
    state.busy = true;
    message("处理中…", false);
    fetch(url, { method: "POST", body: form })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, status: response.status, body: body };
        });
      })
      .then(function (result) {
        state.busy = false;
        if (!result.ok) {
          var code = result.body.error || "失败";
          message(result.status + " " + code + " " + (result.body.message || ""), true);
          return;
        }
        done(result.body);
      })
      .catch(function (err) {
        state.busy = false;
        message("请求失败：" + err, true);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("import-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        send("/api/import", pickInput(), function (body) {
          message("批次 " + body.run_id + (body.reused ? " 已复用" : " 已写入"), false);
          window.location.href = "/anomalies";
        });
      });
    }
    var preview = document.getElementById("preview-btn");
    if (preview) {
      preview.addEventListener("click", function () {
        send("/api/import/preview", pickInput(), function (body) {
          message("采信 " + body.accepted_rows + " 行，隔离 " + body.rejected_rows
            + " 行，编码 " + body.encoding, false);
        });
      });
    }
    var rerun = document.getElementById("rerun-btn");
    if (rerun) {
      rerun.addEventListener("click", function () {
        var node = document.getElementById("rerun-msg");
        fetch("/api/attribution/rerun", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}"
        })
          .then(function (response) { return response.json(); })
          .then(function (body) {
            if (node) {
              node.textContent = body.reused ? "复用 " + body.run_id : "新建 " + body.run_id;
            }
          })
          .catch(function () {
            if (node) {
              node.textContent = "重跑失败";
            }
          });
      });
    }

    var revisionForm = document.getElementById("revision-form");
    if (revisionForm) {
      revisionForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var fileNode = document.getElementById("revision-file");
        var msgNode = document.getElementById("revision-msg");
        var resultNode = document.getElementById("revision-result");
        if (!fileNode.files || !fileNode.files.length) {
          msgNode.textContent = "请先选择修订文件";
          msgNode.className = "msg err";
          return;
        }
        var form = new FormData();
        form.append("file", fileNode.files[0]);
        form.append("mode", document.getElementById("revision-mode").value);
        msgNode.textContent = "整版重算与核对中…";
        msgNode.className = "msg";
        fetch("/api/revisions", { method: "POST", body: form })
          .then(function (response) {
            return response.json().then(function (body) {
              return { ok: response.ok, body: body };
            });
          })
          .then(function (result) {
            if (!result.ok) {
              msgNode.textContent = (result.body.error || "失败") + " " + (result.body.message || "");
              msgNode.className = "msg err";
              return;
            }
            var rr = result.body.revision_result || {};
            var impact = rr.affected || {};
            var counts = impact.counts || {};
            if (rr.status === "failed") {
              msgNode.textContent = "切换失败，沿用旧版 " + rr.current_run_id + "：" + (rr.fail_reason || "核对不平");
              msgNode.className = "msg err";
            } else {
              msgNode.textContent = (rr.reused ? "同一修订重试，复用版本 " : "已切换新版本 ") +
                result.body.run_id;
              msgNode.className = "msg";
            }
            resultNode.hidden = false;
            var checks = (rr.verification && rr.verification.checks) || [];
            resultNode.innerHTML =
              "<p>影响键：新增 " + (counts.added || 0) + " / 改写 " + (counts.changed || 0) +
              " / 撤回 " + (counts.withdrawn || 0) + " / 未变 " + (counts.unchanged || 0) + "</p>" +
              "<p>受影响业务日：" + ((impact.affected_dates || []).join(", ") || "—") + "</p>" +
              "<p>核对：" + checks.map(function (c) {
                return c.name + (c.ok ? "平" : "不平");
              }).join("、") + "</p>" +
              '<p><button class="small" type="button" onclick="window.location.reload()">刷新版本链</button></p>';
          })
          .catch(function (err) {
            msgNode.textContent = "请求失败：" + err;
            msgNode.className = "msg err";
          });
      });
    }

    var markButtons = document.querySelectorAll(".review-mark-btn");
    Array.prototype.forEach.call(markButtons, function (button) {
      button.addEventListener("click", function () {
        var runId = button.getAttribute("data-run-id");
        var eventId = button.getAttribute("data-event-id");
        fetch("/api/review/mark", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            run_id: runId, event_id: eventId, verdict: "confirmed",
            note: "经营会复核确认", reviewer: "web"
          })
        })
          .then(function (response) {
            if (!response.ok) {
              throw new Error(response.status);
            }
            return response.json();
          })
          .then(function () {
            var tag = document.createElement("span");
            tag.className = "tag pass";
            tag.textContent = "confirmed";
            button.parentNode.replaceChild(tag, button);
          })
          .catch(function () {
            button.textContent = "标记失败";
          });
      });
    });
  });
})();
