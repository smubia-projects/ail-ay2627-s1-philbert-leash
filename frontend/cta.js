"use strict";

// AI Lodge rate-limit CTA modal (HTTP 429 from the shared rate limiter) and the
// "demo paused" notice (HTTP 503 from its kill switch). Built from DOM nodes and
// classes only, so the page's strict CSP (no inline styles or scripts) still holds.
window.LeashCTA = (() => {
  const CTA = {
    name: "Leash",
    emoji: "\u{1F9AE}",
    accentColor: "#2b4c7e", // set in cta.css as --rl-accent
    headline: "Your agent runs hit their own leash!",
    description:
      "You've run {count} real-AI agents through Leash in this demo, and the demo's limit held just like an agent's budget does. Come back later for more, or build safety-first AI with us.",
    programme: "AI Lodge",
    programmeLink: "https://www.smubia.com/ai-lodge",
    githubLink: "https://github.com/smubia-projects/ail-ay2627-s1-philbert-leash",
    showcaseLink: "https://www.smubia.com/showcase",
  };

  let modal = null;
  let lastFocus = null;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    return node;
  }

  function link(cls, label, href) {
    const a = el("a", "rl-btn " + cls, label);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener";
    a.appendChild(el("span", "rl-arrow", "→"));
    return a;
  }

  function build() {
    const root = el("div", "rl-modal");
    const backdrop = el("div", "rl-backdrop");
    const card = el("div", "rl-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.setAttribute("aria-labelledby", "rl-headline");
    card.setAttribute("aria-describedby", "rl-description");

    const banner = el("div", "rl-banner");
    banner.append(el("div", "rl-circle rl-circle-1"), el("div", "rl-circle rl-circle-2"), el("span", "rl-emoji", CTA.emoji));
    const headline = el("h2", "rl-headline", CTA.headline);
    headline.id = "rl-headline";
    const desc = el("p", "rl-description");
    desc.id = "rl-description";
    banner.append(headline, desc);

    const body = el("div", "rl-body");
    const dismiss = el("button", "rl-dismiss", "Maybe later");
    dismiss.type = "button";
    body.append(
      link("rl-btn-primary", `Join ${CTA.programme}`, CTA.programmeLink),
      link("rl-btn-secondary", "Self-host from GitHub", CTA.githubLink),
      link("rl-btn-secondary", "Explore other projects", CTA.showcaseLink),
      dismiss
    );
    card.append(banner, body);
    root.append(backdrop, card);
    document.body.appendChild(root);

    backdrop.addEventListener("click", hide);
    dismiss.addEventListener("click", hide);
    root.addEventListener("keydown", (e) => {
      if (e.key === "Escape") return hide();
      if (e.key !== "Tab") return;
      // Keep focus inside the dialog while it is open.
      const items = [...card.querySelectorAll("a, button")];
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
    return { root, desc, first: body.querySelector("a") };
  }

  function showRateLimit(count) {
    modal = modal || build();
    const n = count === undefined || count === null ? "a few" : String(count);
    modal.desc.textContent = CTA.description.replace("{count}", n);
    lastFocus = document.activeElement;
    modal.root.classList.add("active");
    modal.first.focus();
  }

  function hide() {
    if (!modal) return;
    modal.root.classList.remove("active");
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  let notice = null;
  function showPaused(message) {
    if (!notice) {
      notice = el("div", "paused-notice");
      notice.setAttribute("role", "status");
      const text = el("p", "paused-text");
      const close = el("button", "paused-close", "Dismiss");
      close.type = "button";
      close.addEventListener("click", () => notice.classList.remove("active"));
      notice.append(el("strong", "paused-title", "Demo temporarily paused"), text, close);
      document.body.appendChild(notice);
    }
    notice.querySelector(".paused-text").textContent = message || "This demo is temporarily paused. Check back soon.";
    notice.classList.add("active");
  }

  return { showRateLimit, showPaused, hide };
})();
