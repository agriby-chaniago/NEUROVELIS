"use strict";

function deleteRespondent(id, btn) {
  if (!window.confirm(`Delete ${id}?`)) return;

  fetch(`/experiment/respondents/${id}`, { method: "DELETE" })
    .then((r) => r.json())
    .then((data) => {
      if (data.status === "ok") {
        const row = btn.closest("tr");
        if (row) row.remove();
      } else {
        window.alert(`Error: ${data.status}`);
      }
    })
    .catch((err) => {
      window.alert(`Error: ${err}`);
    });
}

window.deleteRespondent = deleteRespondent;
