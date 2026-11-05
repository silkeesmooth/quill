// Quill's Chrome panel. It asks Quill on this computer (native messaging, no
// window) what the PDF in this tab needs, then fills and signs it. A PDF from
// the web is downloaded first, so Quill itself never goes online.
// Labels come from the PDF, so they're only ever inserted as text.

const HOST = "com.silkeesmooth.quill";
const $ = (id) => document.getElementById(id);
let pdfPath = "";
let info = null;
let spot = 0; // which signature line, from Quill's list (best guess first)

function quill(msg) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST, msg, (reply) => {
      if (chrome.runtime.lastError || !reply) {
        reject(new Error("Quill isn't connected to Chrome. Run install.ps1, then reload the extension."));
      } else if (reply.error) {
        reject(new Error(reply.error));
      } else {
        resolve(reply);
      }
    });
  });
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

function note(text, error = false) {
  $("note").textContent = text;
  $("note").className = error ? "note error" : "note";
  $("note").hidden = !text;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

async function localPath(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    throw new Error("Open a PDF in this tab first.");
  }
  if (u.protocol === "file:") {
    if (u.host) throw new Error("Quill only opens PDFs on this computer, not on network shares.");
    return decodeURIComponent(u.pathname).replace(/^\/(?=[A-Za-z]:)/, "").replaceAll("/", "\\");
  }
  if (u.protocol === "http:" || u.protocol === "https:") {
    note("Downloading");
    return download(url);
  }
  throw new Error("Open a PDF in this tab first.");
}

function download(url) {
  return new Promise((resolve, reject) => {
    chrome.downloads.download({ url, conflictAction: "uniquify" }, (id) => {
      if (chrome.runtime.lastError || id === undefined) {
        reject(new Error("Couldn't download this PDF."));
        return;
      }
      const watch = (delta) => {
        if (delta.id !== id || !delta.state) return;
        chrome.downloads.onChanged.removeListener(watch);
        if (delta.state.current === "complete") {
          chrome.downloads.search({ id }, (items) => resolve(items[0].filename));
        } else {
          reject(new Error("The download didn't finish."));
        }
      };
      chrome.downloads.onChanged.addListener(watch);
    });
  });
}

function row(title, detail, onClick) {
  return el("button", { type: "button", className: "row", onclick: onClick }, title,
            detail ? el("small", { textContent: detail }) : "");
}

function spotPicker() {
  // Only worth showing when there's more than one line to choose from.
  if (info.spots.length < 2) return null;
  const select = el("select", {
    onchange: () => {
      spot = Number(select.value);
      if (!$("menu").hidden) showMenu(); // the rows say where they'll sign
    },
  });
  info.spots.forEach((s, i) => select.append(el("option", { value: i, textContent: `${s.label} (page ${s.page})` })));
  select.value = String(spot);
  return el("label", { className: "q" }, el("span", { textContent: "Signature line" }), select);
}

function showMenu() {
  $("questions").hidden = true;
  const menu = $("menu");
  menu.replaceChildren();
  menu.hidden = false;
  const fromDetails = info.auto.length
    ? `${plural(info.auto.length, "field")} from your details`
    : info.details ? "Nothing here matches your details" : "No saved details yet";
  const rest = info.questions.length + info.unrelated.length;

  if (info.auto.length) menu.append(row("Auto-fill", fromDetails, () => fill({}, false)));
  const where = info.canSign ? `On ${info.spots[spot].label}, page ${info.spots[spot].page}` : "";
  if (info.auto.length && info.canSign) menu.append(row("Auto-fill and sign", where, () => fill({}, true)));
  if (rest) menu.append(row("Fill in…", info.questions.length ? plural(info.questions.length, "question") : "Only unrelated questions left", showQuestions));
  if (info.canSign) menu.append(row("Sign only", where, () => fill({}, true, true)));
  if (info.flat) menu.append(el("div", { className: "sep" }), row("Make fillable", "Add fields at the blanks, nothing else", makeFillable));
  const picker = spotPicker();
  if (picker) menu.append(el("div", { className: "sep" }), el("form", {}, picker));

  if (!menu.children.length) {
    note("Nothing to fill in this PDF.");
  } else if (!info.details) {
    note("Save your details from the Start menu (My details for PDF forms) and they fill in by themselves.");
  } else {
    note("");
  }
}

function field(q) {
  if (q.type === "checkbox") {
    return el("label", { className: "check" }, el("input", { type: "checkbox", name: q.name, value: "yes" }),
              el("span", { textContent: q.label }));
  }
  let input;
  if (q.options && q.options.length && q.type !== "text") {
    input = el("select", { name: q.name }, el("option", { value: "", textContent: "" }));
    for (const o of q.options) input.append(el("option", { value: o, textContent: o }));
  } else {
    input = el("input", { type: "text", name: q.name, value: q.value || "", autocomplete: "off" });
  }
  return el("label", { className: "q" }, el("span", { textContent: q.label }), input);
}

function showQuestions() {
  $("menu").hidden = true;
  note("");
  const form = $("questions");
  form.replaceChildren();
  form.hidden = false;
  for (const q of info.questions) form.append(field(q));
  if (info.unrelated.length) {
    // About someone else or for office use: left blank unless opened.
    const more = el("details", {}, el("summary", {
      textContent: `${plural(info.unrelated.length, "more question")} about other people or office use`,
    }));
    for (const q of info.unrelated) more.append(field(q));
    form.append(more);
  }
  let sign = null;
  if (info.canSign) {
    sign = el("input", { type: "checkbox", checked: true });
    form.append(el("label", { className: "check" }, sign, el("span", { textContent: "Sign it" })));
    const picker = spotPicker();
    if (picker) form.append(picker);
  }
  form.append(el("div", { className: "buttons" },
    el("button", { type: "button", textContent: "Back", onclick: showMenu }),
    el("button", { type: "submit", textContent: "Fill in" })));
  form.onsubmit = (e) => {
    e.preventDefault();
    const answers = {};
    for (const input of form.querySelectorAll("input[name], select[name]")) {
      const group = input.closest("details");
      if (group && !group.open) continue; // the skipped group stays blank
      const value = input.type === "checkbox" ? (input.checked ? "yes" : "") : input.value.trim();
      if (value) answers[input.name] = value;
    }
    fill(answers, sign ? sign.checked : false);
  };
  const first = form.querySelector("input, select");
  if (first) first.focus();
}

function busy(text) {
  for (const b of document.querySelectorAll("button")) b.disabled = true;
  note(text);
}

function finished(text) {
  $("menu").hidden = true;
  $("questions").hidden = true;
  note(text);
}

async function fill(answers, sign, signOnly = false) {
  busy(sign ? "Signing" : "Filling in");
  try {
    const r = await quill({ cmd: "fill", path: pdfPath, answers: signOnly ? {} : answers, sign, spot, signOnly });
    finished(`Saved ${r.saved.split("\\").pop()}. It's open in a new tab.`);
  } catch (e) {
    showMenu();
    note(e.message, true);
  }
}

async function makeFillable() {
  busy("Finding the blanks");
  try {
    const r = await quill({ cmd: "make_fillable", path: pdfPath });
    finished(`Added ${plural(r.fields, "field")}. ${r.saved.split("\\").pop()} is open in a new tab.`);
  } catch (e) {
    showMenu();
    note(e.message, true);
  }
}

(async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    pdfPath = await localPath(tab && tab.url ? tab.url : "");
    note("Reading the form");
    info = await quill({ cmd: "inspect", path: pdfPath });
    $("file").textContent = info.file;
    $("file").title = info.file;
    showMenu();
  } catch (e) {
    note(e.message, true);
  }
})();
