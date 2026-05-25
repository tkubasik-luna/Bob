import { Component, type ErrorInfo, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null; info: ErrorInfo | null; copied: boolean };

export class DebugErrorBoundary extends Component<Props, State> {
  state: State = { error: null, info: null, copied: false };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    this.setState({ info });
    console.error("[DebugView] render crash", error, info);
  }

  reset = (): void => {
    this.setState({ error: null, info: null, copied: false });
  };

  copy = async (): Promise<void> => {
    const { error, info } = this.state;
    if (error === null) return;
    const text = [
      `Error: ${error.name}: ${error.message}`,
      "",
      "Stack:",
      error.stack ?? "(no stack)",
      "",
      "Component stack:",
      info?.componentStack ?? "(no component stack)",
    ].join("\n");
    try {
      await navigator.clipboard.writeText(text);
      this.setState({ copied: true });
      setTimeout(() => this.setState({ copied: false }), 1500);
    } catch (e) {
      console.error("clipboard write failed", e);
    }
  };

  render(): ReactNode {
    const { error, info, copied } = this.state;
    if (error === null) return this.props.children;
    return (
      <div
        style={{
          padding: 16,
          background: "#1a0606",
          color: "#ffb3b3",
          fontFamily: "ui-monospace, monospace",
          fontSize: 12,
          lineHeight: 1.4,
          overflow: "auto",
          height: "100%",
        }}
      >
        <div style={{ display: "flex", gap: 8, marginBottom: 12 }}>
          <strong style={{ color: "#ff6b6b" }}>DebugView render crash</strong>
          <button type="button" onClick={this.copy} style={btnStyle}>
            {copied ? "Copié ✓" : "Copier l'erreur"}
          </button>
          <button type="button" onClick={this.reset} style={btnStyle}>
            Réessayer
          </button>
        </div>
        <pre style={{ whiteSpace: "pre-wrap", margin: 0 }}>
          {`${error.name}: ${error.message}\n\n${error.stack ?? ""}`}
          {info?.componentStack ? `\nComponent stack:${info.componentStack}` : ""}
        </pre>
      </div>
    );
  }
}

const btnStyle = {
  padding: "2px 8px",
  background: "#3a1010",
  border: "1px solid #ff6b6b",
  color: "#ffb3b3",
  cursor: "pointer",
  fontFamily: "inherit",
  fontSize: 11,
} as const;
