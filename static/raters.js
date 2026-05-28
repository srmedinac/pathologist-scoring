// Manage the email <-> rater bindings used to auto-route Cloudflare-Access
// users into /review without the radio picker. Edits config.json server-side.

const $ = (id) => document.getElementById(id);

function setMsg(text, ok) {
  const el = $("msg");
  el.textContent = text;
  el.classList.toggle("ok",  !!ok);
  el.classList.toggle("err", !ok);
  if (ok) setTimeout(() => { el.textContent = ""; }, 3500);
}

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

// row-level save (existing raters)
document.querySelectorAll("#raters-table .row-save").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const tr = btn.closest("tr");
    const name = tr.dataset.name;
    const email = tr.querySelector(".email-input").value.trim();
    btn.disabled = true;
    try {
      await api("/admin/raters/email", { name, email });
      setMsg(`Saved ${name}` + (email ? ` → ${email}` : " (unbound)"), true);
    } catch (e) {
      setMsg(`Error saving ${name}: ${e.message}`, false);
    } finally {
      btn.disabled = false;
    }
  });
});

// add new rater
$("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name  = $("new-name").value.trim().toLowerCase();
  const email = $("new-email").value.trim().toLowerCase();
  const submit = e.submitter || e.target.querySelector("button");
  submit.disabled = true;
  try {
    await api("/admin/raters/add", { name, email });
    setMsg(`Added ${name} → ${email}`, true);
    setTimeout(() => location.reload(), 700);
  } catch (e) {
    setMsg(`Error: ${e.message}`, false);
    submit.disabled = false;
  }
});
