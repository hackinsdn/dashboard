/*
 * Shared delete/restore actions for catalog Labs.
 *
 * Used by the Labs view (labs_view.html) and Labs edit (labs_edit.html)
 * pages to avoid duplicating the AJAX call, response handling and toast
 * logic. Depends on jQuery and the AdminLTE Toasts plugin.
 *
 * Exposes window.LabsActions:
 *   - run(opts): perform a delete or restore and handle the response.
 *   - flushToasts(): display any success/failure message stashed in
 *     sessionStorage (used to carry a message across a page reload/redirect).
 */
(function (window, $) {
  "use strict";

  function failMessage(xhr) {
    return xhr.responseJSON ? xhr.responseJSON.result : xhr.responseText;
  }

  // translated UI strings come from the server-rendered window.i18n catalog
  // (see layouts/base.html); fall back to the English key when absent
  function t(key) {
    return (window.i18n && window.i18n[key]) || key;
  }

  var LabsActions = {
    /*
     * opts:
     *   labId       - id of the lab to act on (required)
     *   action      - "delete" (default) or "restore"
     *   redirectUrl - URL to navigate to on success; if omitted, reloads
     *   onFail      - "toast" (default): stash message + reload so the toast
     *                 shows on the reloaded page;
     *                 "alert": show a browser alert and hide the modal
     *   modalId     - modal selector to hide when onFail is "alert"
     */
    run: function (opts) {
      var isRestore = opts.action === "restore";
      var url = "/api/labs/" + opts.labId + (isRestore ? "/restore" : "");
      var request = $.ajax({
        url: url,
        type: isRestore ? "POST" : "DELETE",
        dataType: "json",
        contentType: "application/json",
      });
      request.done(function (data) {
        sessionStorage.setItem("msgSuccess", data.result);
        if (opts.redirectUrl) {
          window.location.href = opts.redirectUrl;
        } else {
          location.reload();
        }
      });
      request.fail(function (xhr) {
        var msg = failMessage(xhr);
        if (opts.onFail === "alert") {
          if (opts.modalId) {
            $(opts.modalId).modal("hide");
          }
          alert(msg);
        } else {
          sessionStorage.setItem("msgFail", msg);
          location.reload();
        }
      });
    },

    flushToasts: function () {
      if (sessionStorage.getItem("msgSuccess")) {
        $(document).Toasts("create", {
          class: "bg-success",
          title: t("Success"),
          body: sessionStorage.getItem("msgSuccess"),
        });
        sessionStorage.removeItem("msgSuccess");
      }
      if (sessionStorage.getItem("msgFail")) {
        $(document).Toasts("create", {
          class: "bg-danger",
          title: t("Failure"),
          body: sessionStorage.getItem("msgFail"),
        });
        sessionStorage.removeItem("msgFail");
      }
    },
  };

  window.LabsActions = LabsActions;
})(window, jQuery);
