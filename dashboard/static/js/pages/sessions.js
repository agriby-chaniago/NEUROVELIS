"use strict";

document.querySelectorAll(".btn-delete").forEach((btn) => {
  btn.addEventListener("click", function () {
    const id = this.dataset.id;
    if (!id) return;

    if (
      !window.confirm(
        `Hapus sesi ${id}?\nSemua data termasuk video dan CSV akan dihapus permanen.`,
      )
    ) {
      return;
    }

    fetch(`/experiment/session/${id}`, { method: "DELETE" })
      .then((r) => r.json())
      .then((data) => {
        if (data.status === "ok") {
          const row = document.getElementById(`row-${id}`);
          if (row) row.remove();
          return;
        }
        window.alert(`Gagal menghapus: ${data.message || data.status}`);
      })
      .catch((err) => {
        window.alert(`Error: ${err}`);
      });
  });
});
