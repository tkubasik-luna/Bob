import { useChatStore } from "../store/chatStore";

/**
 * Top-right toast stack for transient errors received over WS.
 * Auto-dismiss is scheduled in the store (5s) — this component only renders.
 */
export function ToastContainer() {
  const toasts = useChatStore((s) => s.toasts);
  const dismiss = useChatStore((s) => s.dismissToast);

  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none absolute top-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => {
        const isInfo = t.kind === "info";
        const containerCls = isInfo
          ? "pointer-events-auto max-w-sm rounded-md bg-blue-700/90 px-3 py-2 text-left text-sm text-blue-50 shadow-lg ring-1 ring-blue-900/50 hover:bg-blue-700"
          : "pointer-events-auto max-w-sm rounded-md bg-red-700/90 px-3 py-2 text-left text-sm text-red-50 shadow-lg ring-1 ring-red-900/50 hover:bg-red-700";
        const codeCls = isInfo
          ? "mr-2 rounded bg-blue-900/60 px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-blue-100"
          : "mr-2 rounded bg-red-900/60 px-1.5 py-0.5 text-xs font-medium uppercase tracking-wide text-red-100";
        return (
          <button key={t.id} type="button" onClick={() => dismiss(t.id)} className={containerCls}>
            {t.code && <span className={codeCls}>{t.code}</span>}
            <span>{t.message}</span>
          </button>
        );
      })}
    </div>
  );
}
