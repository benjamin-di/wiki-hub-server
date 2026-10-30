/* Same-origin browser client. Tokens are never available to this script. */
"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const labels = {read: "Viewer", contribute: "Contributor", write: "Editor", admin: "Manager", none: "No access"};
  const descriptions = {
    read: "Read pages, sources and history.",
    contribute: "Also submit sources. Accepted ingests save automatically.",
    write: "Also edit through Git or drive and restore content history.",
    admin: "Also share this wiki with other people and manage its settings."
  };
  let stopped = false;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function message(text, error = false, id = "message") {
    const node = $(id);
    if (node) { node.textContent = text; node.classList.toggle("error", error); }
  }
  function clearProtected(text) {
    stopped = true;
    document.title = "Access unavailable · Wiki Hub";
    const area = element("section", undefined, "welcome");
    area.append(element("h1", "Access unavailable"), element("p", text, "muted"));
    const back = element("a", "Go to My wikis", "button"); back.href = "/"; area.append(back);
    $("credential-values")?.replaceChildren();
    $("credential-dialog")?.close();
    $("main").replaceChildren(area);
  }
  async function request(path, data) {
    const headers = {Accept: "application/json"};
    if (data !== undefined) {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = document.querySelector('meta[name="csrf-token"]')?.content || "";
    }
    const response = await fetch(path, {method: data === undefined ? "GET" : "POST", headers,
      credentials: "same-origin", cache: "no-store", body: data === undefined ? undefined : JSON.stringify(data)});
    const value = await response.json();
    if (response.status === 401 || (response.status === 403 && value.access_lost)) {
      clearProtected(value.error || "Your access has changed. Sign in again to continue.");
    }
    if (!response.ok) {
      const error = new Error(value.error || "This request could not be completed.");
      error.status = response.status; throw error;
    }
    return value;
  }
  for (const button of document.querySelectorAll(".close-dialog")) {
    button.addEventListener("click", () => button.closest("dialog").close());
  }
  $("logout")?.addEventListener("click", async () => {
    try { await request("/auth/logout", {}); location.assign("/"); }
    catch (error) { message(error.message, true); }
  });
  const filterWikis = () => {
    const query = $("wiki-search").value.toLocaleLowerCase();
    let count = 0;
    for (const row of document.querySelectorAll("[data-wiki-name]")) {
      row.hidden = !row.dataset.wikiName.toLocaleLowerCase().includes(query)
        || (row.dataset.archived === "true" && !$("show-archived").checked);
      if (!row.hidden) count++;
    }
    $("wiki-no-match").hidden = count !== 0;
  };
  $("wiki-search")?.addEventListener("input", filterWikis);
  $("show-archived")?.addEventListener("change", filterWikis);
  if ($("wiki-search")) filterWikis();
  $("create-open")?.addEventListener("click", () => $("create-dialog").showModal());
  $("create-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const value = await request("/ui/api/wikis", {slug: $("wiki-slug").value, title: $("wiki-title").value});
      location.assign("/wikis/" + encodeURIComponent(value.slug) + "/access");
    } catch (error) { message(error.message, true, "create-error"); button.disabled = false; }
  });

  $("credential-close")?.addEventListener("click", () => {
    $("credential-values").replaceChildren(); $("credential-dialog").close(); location.reload();
  });
  $("credential-dialog")?.addEventListener("cancel", event => {
    // Do not silently lose a one-time credential with Escape.
    event.preventDefault(); $("credential-close").focus();
  });
  for (const form of document.querySelectorAll(".manage-form")) {
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      const button = event.submitter;
      button.disabled = true;
      const data = {};
      for (const input of form.querySelectorAll("[name]")) {
        data[input.name] = input.type === "checkbox" ? input.checked
          : input.hasAttribute("data-boolean") ? input.value === "true" : input.value;
      }
      try {
        const result = await request(form.dataset.endpoint, data);
        if (stopped) return;
        if (result.credential || result.password) {
          const values = $("credential-values"); values.replaceChildren();
          const fields = result.credential ? {"Agent ID": result.id, "Agent key": result.credential}
            : {"Drive": result.unc, "Username": result.login || result.username, "Password": result.password};
          for (const [label, value] of Object.entries(fields)) {
            values.append(element("dt", label), element("dd", value));
          }
          $("credential-dialog").showModal(); $("credential-close").focus();
        } else if (result.pending) {
          message("This update is pending. Access may retain its previous backend state. Refresh to check, or retry the same setting to finish.", true);
          const refresh = element("a", "Refresh status", "button"); refresh.href = location.href;
          $("message").append(element("br"), refresh);
        } else location.reload();
      } catch (error) { if (!stopped) message(error.message, true); }
      finally { button.disabled = false; }
    });
  }

  const root = $("access-page");
  if (!root) {
    if (!document.querySelector('meta[name="csrf-token"]')) return;
    let checking = false;
    const revalidate = async () => {
      if (checking || stopped || document.hidden) return;
      checking = true;
      try { await request("/ui/api/session?page=" + encodeURIComponent(location.pathname)); }
      catch (error) { if (!stopped) message(error.message, true); }
      finally { checking = false; }
    };
    window.addEventListener("focus", revalidate);
    window.addEventListener("pageshow", event => { if (event.persisted) revalidate(); });
    document.addEventListener("visibilitychange", revalidate);
    return;
  }
  const base = "/ui/api/wikis/" + encodeURIComponent(root.dataset.wiki);
  let state = null, dirty = false, selected = null, searchSerial = 0, searchTimer, pollTimer, polls = 0;
  let confirmation = null, loadSerial = 0;
  function who(person) { return person?.name === "Unknown person" ? person.subject : person?.name || "Unknown person"; }
  function details(person) { return person.email || person.username || person.subject; }
  function roleSelect(value, agent, name) {
    const select = element("select"); select.setAttribute("aria-label", "Role for " + name);
    for (const key of agent ? ["read", "contribute"] : ["read", "contribute", "write", "admin"]) {
      const option = element("option", labels[key]); option.value = key; select.append(option);
    }
    select.value = value; return select;
  }
  function confirmChange(title, description, help, action, destructive = false) {
    $("confirm-title").textContent = title;
    $("confirm-description").textContent = description;
    $("confirm-help").textContent = help;
    $("confirm-apply").textContent = destructive ? "Remove access" : "Save change";
    $("confirm-apply").className = "button " + (destructive ? "danger" : "primary");
    confirmation = action; $("confirm-dialog").showModal();
    $("confirm-dialog").querySelector(".close-dialog").focus();
  }
  $("confirm-apply").addEventListener("click", () => {
    $("confirm-dialog").close(); const action = confirmation; confirmation = null; action?.();
  });
  function personRow(person) {
    const row = element("div", undefined, "member");
    const avatar = element("div", (person.name || "?").slice(0, 1).toUpperCase(), "avatar");
    avatar.setAttribute("aria-hidden", "true");
    const text = element("div", undefined, "member-name");
    text.append(element("strong", who(person) + (person.subject === root.dataset.subject ? " (you)" : "")),
      element("span", details(person), "small muted"));
    row.append(avatar, text); return row;
  }
  function renderMembers() {
    const list = $("member-list"); list.replaceChildren();
    const owner = personRow(state.owner_profile);
    const fixed = element("span", "Owner · fixed", "pill");
    fixed.title = "Owner access cannot be changed by sharing"; owner.append(fixed); list.append(owner);
    for (const grant of state.grants) {
      const row = personRow(grant.profile); row.dataset.member = grant.subject;
      if (grant.profile.agent) {
        row.querySelector(".member-name").append(element("span", "Agent · owner: " + grant.profile.owner, "small muted"));
      }
      if (!grant.pending && grant.effective_role !== grant.role) {
        row.querySelector(".member-name").append(element("span", "Effective access: "
          + (labels[grant.effective_role] || "No access") + ". Current account, owner or archive restrictions apply.", "small muted"));
      }
      if (grant.pending) {
        row.classList.add("has-pending");
        row.append(element("span", grant.role === "none" ? "Removal pending" : labels[grant.role] + " pending", "pill"));
        const notice = element("div", undefined, "pending-notice");
        notice.append(element("p", "Wiki API access is blocked while this change is pending. Git or drive access may remain until the update completes."));
        const retry = element("button", "Retry update", "button"); retry.type = "button";
        const operation = state.pending_operations.find(op => op.subject === grant.subject);
        retry.addEventListener("click", () => mutate(grant.subject, grant.role, retry, operation?.id, Boolean(operation)));
        notice.append(retry); row.append(notice);
      } else {
        const controls = element("div", undefined, "member-controls");
        const select = roleSelect(grant.role, grant.profile.agent, who(grant.profile));
        const save = element("button", "Save", "button primary"); save.type = "button"; save.hidden = true;
        select.addEventListener("change", () => {
          save.hidden = select.value === grant.role;
          dirty = Array.from(list.querySelectorAll(".member-controls .primary")).some(button => !button.hidden);
          select.title = descriptions[select.value];
        });
        const apply = () => mutate(grant.subject, select.value, save);
        save.addEventListener("click", () => {
          if (grant.subject === root.dataset.subject && select.value !== "admin") {
            confirmChange("Change your own access?", "You will no longer be able to manage access to this wiki.",
              "Another manager or the owner would need to restore your Manager role.", apply);
          } else if (select.value === "admin") {
            confirmChange("Make " + who(grant.profile) + " a Manager?",
              "They will be able to share this wiki with other people and manage its settings.", "", apply);
          } else apply();
        });
        const remove = element("button", "Remove", "button subtle danger"); remove.type = "button";
        remove.setAttribute("aria-label", "Remove access for " + who(grant.profile));
        remove.addEventListener("click", () => confirmChange("Remove access?",
          "Remove " + who(grant.profile) + " from this wiki?",
          "This also stops new managed copies. Previously downloaded content cannot be recalled.",
          () => mutate(grant.subject, "none", remove), true));
        controls.append(select, save, remove); row.append(controls);
      }
      list.append(row);
    }
  }
  function renderActivity() {
    const list = $("activity-list"); list.replaceChildren();
    if (!state.activity.length) list.append(element("p", "No access changes have been recorded yet.", "muted small"));
    for (const activity of state.activity) {
      const row = element("div", undefined, "activity-item");
      const confirmed = activity.status === "completed";
      const role = activity.role === "none"
        ? (confirmed ? "removed access for " : "requested removal of access for ")
        : (confirmed ? "set " : "requested ") + labels[activity.role] + " access for ";
      row.append(element("span", who(activity.actor_profile) + " " + role + who(activity.target_profile) + "."));
      const time = element("time", new Date(activity.created).toLocaleString() + " · " + activity.status, "activity-time");
      time.dateTime = activity.created; row.append(time); list.append(row);
    }
  }
  function schedulePoll() {
    clearTimeout(pollTimer);
    if (state?.grants.some(grant => grant.pending) && polls < 20 && !stopped) {
      pollTimer = setTimeout(() => { polls++; if (!document.hidden) loadAccess(); else schedulePoll(); }, 3000);
    }
  }
  function memberEdits(exclude) {
    const edits = new Map();
    for (const row of document.querySelectorAll("[data-member]")) {
      const select = row.querySelector("select");
      const grant = state?.grants.find(item => item.subject === row.dataset.member);
      if (select && grant && grant.subject !== exclude && select.value !== grant.role) {
        edits.set(grant.subject, {before: grant.role, role: select.value});
      }
    }
    return edits;
  }
  async function loadAccess(force = false, savedSubject = null) {
    if (stopped) return;
    const serial = ++loadSerial;
    const edits = savedSubject === null ? new Map() : memberEdits(savedSubject);
    try {
      const current = await request(base + "/access");
      // Focus, polling and mutation recovery can overlap. An older reply must
      // never replace newer membership or an edit made against that membership.
      if (stopped || serial !== loadSerial || (state && current.version < state.version)) return;
      const hasDraft = dirty || $("add-dialog").open || $("confirm-dialog").open;
      if (hasDraft && state && !force) {
        if (current.version !== state.version) $("stale-notice").hidden = false;
        return;
      }
      $("add-open").disabled = false;
      $("agent-share-form")?.querySelector("button").removeAttribute("disabled");
      state = current; dirty = false; $("stale-notice").hidden = true;
      renderMembers(); renderActivity();
      for (const row of document.querySelectorAll("[data-member]")) {
        const edit = edits.get(row.dataset.member), select = row.querySelector("select");
        if (edit && select && select.value === edit.before) {
          select.value = edit.role;
          select.dispatchEvent(new Event("change"));
        }
      }
      schedulePoll();
    } catch (error) { if (!stopped && serial === loadSerial) message(error.message, true); }
  }
  async function mutate(subject, role, button, identifier, retry = false, feedbackId = "message") {
    if (!state || stopped) return;
    button.disabled = true; message("Applying access change…", false, feedbackId);
    const operationId = identifier || crypto.randomUUID();
    try {
      const result = await request(base + "/access", {subject, role, version: state.version,
        operation_id: operationId, retry});
      dirty = false; polls = 0;
      message(result.operation.status === "pending"
        ? "This update is pending. Retry when the backend is available."
        : "Access change confirmed. Current access is shown below.");
      await loadAccess(true, subject);
      return result.operation;
    } catch (error) {
      if (!stopped) {
        if (error.status === 409) {
          message(error.message, true, feedbackId); $("stale-notice").hidden = false;
        } else if (!error.status) {
          dirty = false;
          await loadAccess(true, subject);
          try {
            const observed = await request(base + "/operation?id=" + encodeURIComponent(operationId));
            if (["completed", "pending"].includes(observed.operation.status)) {
              message(observed.operation.status === "completed"
                ? "The recorded change is confirmed. Current access is shown below."
                : "The recorded change is pending. Retry when the backend is available.");
              return observed.operation;
            }
          } catch { /* Keep an unobservable outcome explicit; never repeat a mutation here. */ }
          if (!stopped) message("The reply was lost. Check current access and activity before making another change.", true, feedbackId);
        } else message(error.message, true, feedbackId);
      }
    } finally { button.disabled = false; }
  }
  $("agent-share-form")?.addEventListener("submit", async event => {
    event.preventDefault();
    await mutate($("share-agent").value, $("share-agent-role").value, event.submitter);
  });
  $("refresh-access").addEventListener("click", () => { polls = 0; loadAccess(true); });
  $("activity-refresh").addEventListener("click", () => { polls = 0; loadAccess(); });
  $("copy-link").addEventListener("click", async () => {
    const url = location.origin + "/wikis/" + encodeURIComponent(root.dataset.wiki);
    try { await navigator.clipboard.writeText(url); message("Wiki link copied. It works only for people who already have access."); }
    catch { message("Wiki link: " + url + " — existing access required."); }
  });
  $("add-open").addEventListener("click", () => {
    $("add-form").reset(); selected = null; $("selected-person").hidden = true;
    $("people-results").replaceChildren(); $("add-submit").disabled = true;
    message("", false, "add-error"); message("", false, "search-status");
    $("role-description").textContent = descriptions.read;
    $("add-dialog").showModal(); $("person-search").focus();
  });
  $("add-dialog").addEventListener("close", () => { searchSerial++; clearTimeout(searchTimer); });
  $("new-role").addEventListener("change", () => { $("role-description").textContent = descriptions[$("new-role").value]; });
  $("person-subject").addEventListener("input", () => {
    selected = $("person-subject").value.trim() ? {subject: $("person-subject").value.trim()} : null;
    $("selected-person").hidden = true; $("add-submit").disabled = !selected;
  });
  $("person-search").addEventListener("input", () => {
    clearTimeout(searchTimer); const serial = ++searchSerial;
    selected = null; $("selected-person").hidden = true; $("add-submit").disabled = true;
    $("people-results").replaceChildren();
    const query = $("person-search").value.trim();
    if (query.length < 2) { message("Type at least two characters.", false, "search-status"); return; }
    searchTimer = setTimeout(async () => {
      message("Searching…", false, "search-status");
      try {
        const result = await request(base + "/people?q=" + encodeURIComponent(query));
        if (serial !== searchSerial || stopped) return;
        $("advanced-subject").hidden = result.directory;
        message(result.people.length ? "Select a person below." : "No matching people found.", false, "search-status");
        $("search-help").textContent = result.directory ? "Select a person from your identity directory."
          : "Showing people who have signed in before. Use an exact subject for another account.";
        for (const person of result.people) {
          if (person.subject === root.dataset.owner || state?.grants.some(grant => grant.subject === person.subject)) continue;
          const choice = element("button", undefined, "person-result"); choice.type = "button";
          choice.append(element("strong", who(person)), element("span", details(person)));
          choice.addEventListener("click", () => {
            selected = person; $("people-results").replaceChildren();
            $("selected-person").textContent = who(person) + " · " + details(person);
            $("selected-person").hidden = false; $("add-submit").disabled = false;
            message("Person selected.", false, "search-status"); $("new-role").focus();
          });
          $("people-results").append(choice);
        }
      } catch (error) { if (serial === searchSerial) message(error.message, true, "search-status"); }
    }, 300);
  });
  $("add-form").addEventListener("submit", async event => {
    event.preventDefault(); if (!selected || !state) return;
    const person = selected, role = $("new-role").value;
    const result = await mutate(person.subject, role, $("add-submit"), undefined, false, "add-error");
    if (result && !stopped) $("add-dialog").close();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !stopped) { polls = 0; loadAccess(); } });
  window.addEventListener("focus", () => { if (!stopped) loadAccess(); });
  loadAccess();
})();
