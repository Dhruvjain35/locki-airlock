import { lazy, Suspense, useEffect, useState } from "react";

const LegacyApp = lazy(() => import("./LegacyApp"));
const AirlockApp = lazy(() => import("./airlock/AirlockApp"));
const LandingPage = lazy(() => import("./landing/LandingPage"));

/** The console remains the default; the product preview loads separately from live views. */
export default function App() {
  const read = () => {
    if (location.hash === "#demo") return "legacy";
    const product = new URLSearchParams(window.location.search).get("product");
    if (product === "landing") return "landing";
    return product === "legacy" ? "legacy" : "console";
  };
  const [view, setView] = useState(read);
  useEffect(() => {
    const on = () => setView(read());
    window.addEventListener("hashchange", on);
    window.addEventListener("popstate", on);
    return () => {
      window.removeEventListener("hashchange", on);
      window.removeEventListener("popstate", on);
    };
  }, []);
  return <Suspense fallback={<div role="status" style={{ padding: "3rem" }}>Loading…</div>}>
    {view === "landing" ? <LandingPage /> : view === "legacy" ? <LegacyApp /> : <AirlockApp />}
  </Suspense>;
}
