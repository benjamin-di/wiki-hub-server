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
  let customSlug = false;
  const fallbackSlug = "wiki-" + crypto.randomUUID().slice(0, 8);
  function suggestedSlug() {
    let value = $("wiki-title").value.normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (!value) return fallbackSlug;
    if (!/^[a-z]/.test(value)) value = "wiki-" + value;
    return value.slice(0, 63).replace(/-$/g, "");
  }
  $("create-open")?.addEventListener("click", () => {
    if (!customSlug) $("wiki-slug").value = suggestedSlug();
    $("create-dialog").showModal(); $("wiki-title").focus();
  });
  $("wiki-title")?.addEventListener("input", () => {
    if (!customSlug) $("wiki-slug").value = suggestedSlug();
  });
  $("wiki-slug")?.addEventListener("input", () => { customSlug = true; });
  $("wiki-slug")?.addEventListener("invalid", () => { $("create-advanced").open = true; });
  $("create-form")?.addEventListener("submit", async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const value = await request("/ui/api/wikis", {slug: $("wiki-slug").value,
        title: $("wiki-title").value.trim(), purpose: $("wiki-purpose").value.trim()});
      location.assign("/wikis/" + encodeURIComponent(value.slug));
    } catch (error) {
      if (stopped) return;
      message(error.message, true, "create-error");
      if (error.status === 409) { $("create-advanced").open = true; $("wiki-slug").focus(); }
      if (!error.status || error.status >= 500) {
        message("Creation could not be confirmed. Check the wiki before trying again.", true, "create-error");
        const link = element("a", "Check wiki", "button");
        link.href = "/wikis/" + encodeURIComponent($("wiki-slug").value); $("create-error").append(link);
      }
      button.disabled = false;
    }
  });

  $("credential-close")?.addEventListener("click", () => {
    $("credential-values").replaceChildren(); $("credential-dialog").close(); location.reload();
  });
  $("credential-dialog")?.addEventListener("cancel", event => {
    // Do not silently lose a one-time credential with Escape.
    event.preventDefault(); $("credential-close").focus();
  });
  for (const form of document.querySelectorAll(".manage-form")) {
    let submitting = false, reviewRequired = false;
    function review(text, label = "Refresh status", href = location.href) {
      message(text, true);
      const link = element("a", label, "button"); link.href = href;
      $("message").append(element("br"), link);
    }
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (submitting || reviewRequired || stopped) return;
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) return;
      const button = event.submitter || form.querySelector('[type="submit"]');
      submitting = true;
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
          review(form.dataset.pendingMessage || "This update is pending. Refresh its status before retrying.");
        } else location.reload();
      } catch (error) {
        if (!stopped) {
          if (!error.status || error.status >= 500) {
            if (form.dataset.registration) {
              reviewRequired = true;
              const agent = form.dataset.registration === "agent";
              review(agent
                ? (data.client_id ? "The agent registration could not be confirmed. Review your agents before registering again."
                  : "The agent may have been registered, but its key was not received. Review your agents and revoke the new registration before trying again.")
                : "The device may have been created, but its password was not received. Review your devices and revoke the new device before creating another.",
                agent ? "Review agents" : "Review devices", agent ? "/agents" : "/connections");
            } else {
              review("The result could not be confirmed. Refresh status before retrying; the change may already have been applied.");
            }
          } else message(error.message, true);
        }
      } finally { submitting = false; button.disabled = reviewRequired; }
    });
  }

  const refreshNames = $("refresh-account-names");
  if (refreshNames) {
    const refreshAccountNames = async () => {
      if (refreshNames.disabled || stopped) return;
      refreshNames.disabled = true;
      message("Updating account names…", false, "profile-message");
      try {
        const result = await request("/ui/api/accounts/profiles", {});
        if (stopped) return;
        const people = new Map(result.people.map(person => [person.subject, person]));
        for (const row of document.querySelectorAll("[data-account]")) {
          const person = people.get(row.dataset.account);
          if (!person) continue;
          row.querySelector("[data-account-name]").textContent = person.name;
          row.querySelector("[data-account-detail]").textContent = person.email || person.username || person.subject;
        }
        message(result.unavailable
          ? "Some account names could not be refreshed. Saved names and account settings remain available. Try Refresh names again shortly."
          : "Account names updated.", Boolean(result.unavailable), "profile-message");
      } catch (error) { if (!stopped) message(error.message, true, "profile-message"); }
      finally { refreshNames.disabled = false; }
    };
    refreshNames.addEventListener("click", refreshAccountNames);
    refreshAccountNames();
  }

  const sources = $("sources-page");
  if (sources) {
    const base = "/ui/api/wikis/" + encodeURIComponent(sources.dataset.wiki);
    const receiptKey = "wiki-submission:" + sources.dataset.subject + ":" + sources.dataset.wiki;
    let pending = null, sending = false, checkingSubmission = false, refreshing = false, refreshAgain = false, timer, polls = 0;
    try { pending = JSON.parse(sessionStorage.getItem(receiptKey)); } catch { /* Storage may be disabled. */ }
    if (!pending || typeof pending.id !== "string" || typeof pending.fingerprint !== "string") pending = null;
    function remember(value) {
      pending = value;
      try {
        if (value) sessionStorage.setItem(receiptKey, JSON.stringify(value));
        else sessionStorage.removeItem(receiptKey);
      } catch { /* The receipt stays in memory; source bytes never enter browser storage. */ }
    }
    function notice(text, error = false) { message(text, error, "source-message"); }
    function accepted(job, clearDraft = true) {
      remember(null);
      $("source-check").hidden = true;
      $("source-different").hidden = true;
      const text = document.querySelector('[name="source-kind"]:checked').value === "text";
      const otherDraft = text ? $("source-file").files.length : $("source-name").value || $("source-text").value;
      if (clearDraft) {
        // Clear only the confirmed source, keeping drafts entered in the other mode.
        if (text) { $("source-name").value = ""; $("source-text").value = ""; }
        else $("source-file").value = "";
        if (otherDraft) {
          document.querySelector('[name="source-kind"][value="' + (text ? "file" : "text") + '"]').checked = true;
          chooseKind();
        }
      }
      notice(clearDraft && !otherDraft ? "Source received. Its progress is shown below; you can leave this page."
        : "Previous source received. Your unsent source is still here and has not been submitted.");
      polls = 0;
    }
    async function refreshSources() {
      if (stopped || document.hidden) return;
      if (refreshing) { refreshAgain = true; return; }
      refreshing = true; clearTimeout(timer);
      try {
        const result = await request(base + "/sources");
        if (stopped) return;
        message("", false, "sources-error");
        if ($("source-form")) $("source-form").hidden = result.access.read_only || result.access.role === "read";
        $("overview-sharing")?.toggleAttribute("hidden", !result.access.manage);
        $("overview-access").textContent = result.access.private ? "Only you have access"
          : result.access.owner ? "Owner" : labels[result.access.role];
        if ($("source-heading")) $("source-heading").textContent = result.has_sources ? "Add a source" : "Add your first source";
        const list = $("source-jobs"); list.replaceChildren();
        const states = {queued: "Waiting to organize", running: "Organizing", publishing: "Saving",
          completed: "Saved automatically", failed: "Needs review · failed", interrupted: "Needs review · interrupted",
          uncertain: "Needs review · save not confirmed"};
        if (!result.jobs.length) list.append(element("p", "No sources submitted yet.", "muted"));
        for (const job of result.jobs) {
          const row = element("article", undefined, "activity-item"); row.dataset.job = job.id;
          row.append(element("strong", job.filename), element("p", states[job.status] || job.status));
          if (job.error) row.append(element("p", job.error, "error"));
          if (["failed", "interrupted", "uncertain"].includes(job.status)) {
            row.append(element("p", "Review this source before submitting it again. It will not be retried automatically.", "help"));
          }
          const detail = element("details"), summary = element("summary", "Job details"); detail.append(summary);
          detail.append(element("p", "Job: " + job.id, "help"));
          if (job.revision) detail.append(element("p", "Accepted revision: " + job.revision, "help"));
          row.append(detail); list.append(row);
        }
        if (result.jobs.some(job => ["queued", "running", "publishing"].includes(job.status)) && polls++ < 40) {
          timer = setTimeout(refreshSources, 3000);
        }
      } catch (error) { if (!stopped) message(error.message, true, "sources-error"); }
      finally {
        refreshing = false;
        if (refreshAgain) { refreshAgain = false; refreshSources(); }
      }
    }
    function chooseKind() {
      const text = document.querySelector('[name="source-kind"]:checked')?.value === "text";
      $("source-file-fields").hidden = text; $("source-text-fields").hidden = !text;
      $("source-file").disabled = text; $("source-file").required = !text;
      for (const id of ["source-name", "source-text"]) { $(id).disabled = !text; $(id).required = text; }
    }
    document.querySelectorAll('[name="source-kind"]').forEach(input => input.addEventListener("change", chooseKind));
    async function checkSubmission() {
      if (!pending || sending || checkingSubmission || stopped) return;
      checkingSubmission = true;
      for (const id of ["source-check", "source-submit", "source-inputs", "source-different"]) $(id).disabled = true;
      try {
        const result = await request(base + "/submission?id=" + encodeURIComponent(pending.id));
        if (stopped) return;
        if (result.job) {
          let clearDraft = !$("source-file").files.length && !$("source-name").value && !$("source-text").value;
          if (!clearDraft) {
            try { clearDraft = (await sourceInput()).fingerprint === pending.fingerprint; }
            catch { /* Keep partial or invalid unsent edits as well. */ }
          }
          accepted(result.job, clearDraft); await refreshSources();
        }
        else notice("No saved submission found yet. Check again or retry the same source; it won't create a duplicate.", true);
      } catch (error) { if (!stopped) notice(error.message, true); }
      finally {
        checkingSubmission = false;
        for (const id of ["source-check", "source-submit", "source-inputs", "source-different"]) {
          if ($(id)) $(id).disabled = false;
        }
      }
    }
    $("source-check")?.addEventListener("click", checkSubmission);
    $("source-different")?.addEventListener("click", () => {
      if (sending || checkingSubmission || stopped || !window.confirm("This clears the current inputs without cancelling the previous submission. It may still finish; check recent activity before adding it again. Start a different source?")) return;
      remember(null); $("source-form").reset(); chooseKind();
      $("source-check").hidden = true; $("source-different").hidden = true;
      notice("Ready for a different source. The previous submission has not been cancelled.");
    });
    $("sources-refresh").addEventListener("click", () => { polls = 0; refreshSources(); });
    async function sourceInput() {
      let bytes, filename;
      if (document.querySelector('[name="source-kind"]:checked').value === "text") {
        filename = $("source-name").value.trim() + ".txt";
        if (!$("source-text").value.trim()) throw new Error("Paste some text before adding this source.");
        bytes = new TextEncoder().encode($("source-text").value);
      } else {
        const file = $("source-file").files[0];
        if (!file || file.size > Number(sources.dataset.limit)) throw new Error("Choose a file within the size limit.");
        filename = file.name; bytes = new Uint8Array(await file.arrayBuffer());
      }
      if (!bytes.length || bytes.length > Number(sources.dataset.limit)) throw new Error("The source is empty or exceeds the size limit.");
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
        value => value.toString(16).padStart(2, "0")).join("");
      const fingerprint = filename + ":" + digest;
      return {bytes, filename, fingerprint};
    }
    $("source-form")?.addEventListener("submit", async event => {
      event.preventDefault(); if (sending || checkingSubmission || stopped) return;
      const recovering = Boolean(pending);
      sending = true; $("source-submit").disabled = true; $("source-inputs").disabled = true;
      let submitted = false;
      try {
        const {bytes, filename, fingerprint} = await sourceInput();
        if (pending && pending.fingerprint !== fingerprint) {
          throw new Error("A previous submission is unconfirmed. Check it first, or retry with the same name and source.");
        }
        if (!pending) remember({id: crypto.randomUUID(), fingerprint});
        let binary = "";
        for (let offset = 0; offset < bytes.length; offset += 8192) {
          binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
        }
        notice("Sending source…");
        submitted = true;
        const job = await request(base + "/sources", {filename, content_base64: btoa(binary), submission_id: pending.id});
        if (stopped) return;
        accepted(job); await refreshSources();
      } catch (error) {
        if (stopped) return;
        notice(error.message, true);
        if (pending && submitted) {
          if (!recovering && error.status && error.status < 500 && error.status !== 429) remember(null);
          else {
            $("source-check").hidden = false;
            $("source-different").hidden = false;
            notice(recovering && error.status && error.status < 500
              ? error.message + " The earlier submission is still unconfirmed. Check its status before retrying."
              : "Submission could not be confirmed. Check its status before retrying. Your source has not been cleared.", true);
            if (!error.status || error.status === 429 || error.status >= 500) {
              polls = 0;
              await refreshSources();
            }
          }
        }
      } finally {
        sending = false;
        if ($("source-submit")) { $("source-submit").disabled = false; $("source-inputs").disabled = false; }
      }
    });
    if (pending && $("source-form")) {
      $("source-check").hidden = false; $("source-different").hidden = false; checkSubmission();
    }
    window.addEventListener("focus", refreshSources);
    window.addEventListener("pageshow", event => { if (event.persisted) refreshSources(); });
    document.addEventListener("visibilitychange", refreshSources);
    refreshSources();
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
      return true;
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
      message("Access change recorded. Refreshing current access…");
      const refreshed = await loadAccess(true, subject);
      if (stopped) return;
      if (!refreshed) {
        message("The change was recorded. Reload access to check its current status before making another change.", true);
        $("stale-notice").hidden = false;
        return result.operation;
      }
      message(result.operation.status === "pending"
        ? "This update is pending. Retry when the backend is available."
        : "Access change confirmed. Current access is shown below.");
      return result.operation;
    } catch (error) {
      if (!stopped) {
        if (error.status === 409) {
          message(error.message, true, feedbackId); $("stale-notice").hidden = false;
        } else if (!error.status) {
          dirty = false;
          message("The reply was lost. Checking current access…");
          const refreshed = await loadAccess(true, subject);
          if (stopped) return;
          try {
            const observed = await request(base + "/operation?id=" + encodeURIComponent(operationId));
            if (["completed", "pending"].includes(observed.operation.status)) {
              if (!refreshed) {
                message("The change was recorded. Reload access to check its current status before making another change.", true);
                $("stale-notice").hidden = false;
                return observed.operation;
              }
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
  window.addEventListener("pageshow", event => { if (event.persisted && !stopped) loadAccess(); });
  loadAccess();
})();
