import type { ComponentDescriptor } from "../types/ws";
import { componentRegistry } from "./registry";

type DispatcherProps = {
  ui: ComponentDescriptor[];
};

/**
 * Renders a flat list of `ComponentDescriptor`s by looking each one up in
 * `componentRegistry`. Unknown component names render a visible warning block
 * rather than throwing, so an out-of-date frontend can never crash the chat.
 */
export function Dispatcher({ ui }: DispatcherProps) {
  if (ui.length === 0) return null;
  return (
    <div className="flex flex-col gap-2">
      {ui.map((descriptor, index) => {
        const Component = componentRegistry[descriptor.component];
        const key = `${descriptor.component}-${index}`;
        if (!Component) {
          return <UnknownComponent key={key} name={descriptor.component} />;
        }
        return <Component key={key} props={descriptor.props} />;
      })}
    </div>
  );
}

function UnknownComponent({ name }: { name: string }) {
  return (
    <div className="rounded-md border border-amber-700/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-200">
      Unknown UI component: <code className="font-mono">{name}</code>
    </div>
  );
}
