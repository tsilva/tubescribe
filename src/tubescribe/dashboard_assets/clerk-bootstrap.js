"use strict";

window.tubeScribeClerkReady ??= (async () => {
  const bootstrap = document.querySelector('meta[name="tubescribe-config"]');
  let settings;
  if (bootstrap) {
    settings = JSON.parse(bootstrap.content);
  } else {
    const response = await fetch("/auth/config", {cache: "no-store"});
    if (!response.ok) throw new Error("Sign-in configuration is unavailable. Refresh to retry.");
    settings = await response.json();
  }
  const frontend = new URL(settings.frontend_api);
  if (frontend.protocol !== "https:" || frontend.pathname !== "/") throw new Error("Invalid sign-in configuration");

  // Wait for the parser, without waiting for the dashboard's deferred script.
  if (document.readyState === "loading") {
    await new Promise(resolve => document.addEventListener("readystatechange", resolve, {once: true}));
  }
  for (const path of settings.browser_scripts) {
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = frontend.origin + path;
      script.crossOrigin = "anonymous";
      script.dataset.clerkPublishableKey = settings.publishable_key;
      script.onload = resolve;
      script.onerror = () => reject(new Error("Sign-in could not load. Refresh to retry."));
      document.head.append(script);
    });
  }
  // Fetch shared UI code while Clerk checks the session. Match the UI loader's request mode.
  for (const path of settings.browser_ui_chunks ?? []) {
    const link = document.createElement("link");
    link.rel = "preload";
    link.as = "script";
    link.href = frontend.origin + path;
    link.fetchPriority = "low";
    document.head.append(link);
  }
  await window.Clerk.load({
    ui: {ClerkUI: window.__internal_ClerkUICtor},
    localization: {signIn: {start: {title: "Sign in to TubeScribe", subtitle: "Your videos, ready to read."}}},
    appearance: {
      variables: {colorPrimary: "#21644e", colorForeground: "#141b29", colorMutedForeground: "#657084", fontFamily: "inherit", borderRadius: "8px"},
      elements: {
        rootBox: "auth-root", cardBox: "auth-card-box", card: "auth-card",
        headerTitle: "auth-title", headerSubtitle: "auth-subtitle",
        socialButtonsBlockButton: "auth-social", formFieldInput: "auth-input",
        formButtonPrimary: "auth-submit", footer: "auth-footer",
      },
    },
  });

  const container = document.getElementById("sign-in");
  let mounted = false;
  function setSignInVisible(visible) {
    if (visible === mounted) return;
    if (visible) {
      window.Clerk.mountSignIn(container, {routing: "hash", forceRedirectUrl: "/" + window.location.search});
    } else {
      window.Clerk.unmountSignIn(container);
    }
    mounted = visible;
  }

  function loadReader() {
    // A document left open across deployment may already include the reader.
    if (document.querySelector('script[src="/app.js"]')) return;
    const script = document.createElement("script");
    script.src = "/app.js";
    script.onerror = () => {
      document.getElementById("auth-message").textContent = "Your library could not load. Refresh to retry.";
    };
    document.head.append(script);
  }

  if (window.Clerk.user && window.Clerk.session) {
    loadReader();
  } else {
    document.getElementById("auth-message").textContent = "";
    setSignInVisible(true);
    const unsubscribe = window.Clerk.addListener(({user, session}) => {
      if (!user || !session) return;
      loadReader();
      // The reader now owns identity changes. Defer cleanup in case Clerk emits synchronously.
      void Promise.resolve().then(() => unsubscribe());
    });
  }
  return setSignInVisible;
})();
void window.tubeScribeClerkReady.catch(error => {
  const report = () => {
    const message = document.getElementById("auth-message");
    if (message) message.textContent = error.message;
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", report, {once: true});
  } else {
    report();
  }
});
