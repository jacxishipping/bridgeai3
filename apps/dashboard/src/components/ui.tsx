import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, X } from "lucide-react";

export function Button({
  variant = "default",
  size = "default",
  className = "",
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "ghost" | "danger";
  size?: "default" | "sm" | "icon";
}) {
  const variantCls =
    variant === "primary"
      ? "btn-primary"
      : variant === "ghost"
        ? "btn-ghost"
        : variant === "danger"
          ? "btn-danger"
          : "";
  const sizeCls = size === "icon" ? "btn-icon" : size === "sm" ? "btn-sm" : "";
  return (
    <button className={`btn ${variantCls} ${sizeCls} ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}

export function Chip({
  active = false,
  className = "",
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      className={`chip ${active ? "active" : ""} ${className}`.trim()}
      {...props}
    >
      {children}
    </button>
  );
}

export function SurfaceCard({
  lift = false,
  className = "",
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { lift?: boolean }) {
  return (
    <div
      className={`surface ${lift ? "surface-lift" : ""} ${className}`.trim()}
      {...props}
    >
      {children}
    </div>
  );
}

export function StatusBadge({
  status,
  label,
}: {
  status: "ok" | "warn" | "err" | "muted";
  label?: string;
}) {
  const text =
    label ??
    (status === "ok"
      ? "Online"
      : status === "warn"
        ? "Pending"
        : status === "err"
          ? "Failed"
          : "Offline");
  const cls =
    status === "ok"
      ? "status-ok"
      : status === "warn"
        ? "status-warn"
        : status === "err"
          ? "status-err"
          : "status-muted";
  const dotBg =
    status === "warn"
      ? "var(--amber)"
      : status === "err"
        ? "var(--rose)"
        : "var(--text-muted)";

  return (
    <span
      className={`mono ${cls}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        fontSize: 12,
        fontWeight: 500,
      }}
    >
      {status === "ok" ? (
        <span className="pulse-dot" />
      ) : (
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: dotBg,
            display: "inline-block",
          }}
        />
      )}
      {text}
    </span>
  );
}

export function Toggle({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label?: string;
  disabled?: boolean;
}) {
  return (
    <label
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.55 : 1,
      }}
    >
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        disabled={disabled}
        className={`toggle ${checked ? "on" : ""}`}
        onClick={() => !disabled && onChange(!checked)}
      />
      {label && <span style={{ fontSize: 12.5 }}>{label}</span>}
    </label>
  );
}

export function UnderlineTabs<T extends string>({
  tabs,
  value,
  onChange,
}: {
  tabs: Array<{ id: T; label: string }>;
  value: T;
  onChange: (next: T) => void;
}) {
  return (
    <div className="tab-bar">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={`tab-underline ${tab.id === value ? "active" : ""}`}
          onClick={() => onChange(tab.id)}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function Sparkline({
  values,
  stroke = "#6366F1",
  height = 44,
}: {
  values: number[];
  stroke?: string;
  height?: number;
}) {
  const safe = values.length >= 2 ? values : [4, 6, 5, 8, 7, 9, 6];
  const max = Math.max(100, ...safe);
  const min = Math.min(0, ...safe);
  const range = Math.max(1, max - min);
  const w = 360;
  const h = height;

  const points = safe.map((v, idx) => {
    const x = (idx / (safe.length - 1)) * w;
    const y = h - 4 - ((v - min) / range) * (h - 8);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const d = `M ${points.join(" L ")}`;
  const gradId = useId();

  return (
    <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height, display: "block" }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity="0.24" />
          <stop offset="100%" stopColor={stroke} stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d} L ${w},${h} L 0,${h} Z`} fill={`url(#${gradId})`} />
      <path
        key={d}
        className="spark-line"
        d={d}
        fill="none"
        stroke={stroke}
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export function TagInput({
  tags,
  onChange,
  placeholder = "Add tag + Enter...",
}: {
  tags: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
}) {
  const [draft, setDraft] = useState("");
  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
      {tags.map((tag, idx) => (
        <span key={`${tag}-${idx}`} className="tag-pill anim-enter mono">
          {tag}
          <button
            type="button"
            aria-label={`Remove ${tag}`}
            onClick={() => onChange(tags.filter((_, i) => i !== idx))}
            style={{
              background: "transparent",
              border: 0,
              color: "var(--text-muted)",
              cursor: "pointer",
              padding: 0,
              display: "inline-flex",
            }}
          >
            <X size={12} />
          </button>
        </span>
      ))}
      <input
        type="text"
        className="field mono"
        style={{ width: 170, padding: "5px 9px", fontSize: 12 }}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && draft.trim()) {
            e.preventDefault();
            const val = draft.trim();
            if (!tags.includes(val)) onChange([...tags, val]);
            setDraft("");
          }
        }}
      />
    </div>
  );
}

export function Accordion({
  items,
}: {
  items: Array<{ title: string; body: string }>;
}) {
  const [openIdx, setOpenIdx] = useState<number | null>(0);
  return (
    <div className="surface" style={{ overflow: "hidden" }}>
      {items.map((item, idx) => {
        const isOpen = openIdx === idx;
        return (
          <div
            key={item.title}
            style={{
              padding: "13px 16px",
              borderTop: idx > 0 ? "1px solid var(--border-hair)" : undefined,
            }}
          >
            <button
              type="button"
              onClick={() => setOpenIdx(isOpen ? null : idx)}
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                background: "transparent",
                border: 0,
                padding: 0,
                color: "var(--text-primary)",
                fontSize: 12.5,
                fontWeight: 500,
                cursor: "pointer",
                textAlign: "left",
              }}
            >
              <span>{item.title}</span>
              <span className="mono" style={{ color: "var(--text-muted)", fontSize: 14 }}>
                {isOpen ? "−" : "+"}
              </span>
            </button>
            {isOpen && (
              <div
                className="anim-enter"
                style={{
                  marginTop: 8,
                  fontSize: 12,
                  lineHeight: 1.55,
                  color: "var(--text-secondary)",
                }}
              >
                {item.body}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export function TimelineFeed({
  items,
}: {
  items: Array<{ id: string | number; title: string; meta: string; color?: string }>;
}) {
  if (items.length === 0) {
    return (
      <div style={{ fontSize: 12, color: "var(--text-muted)", padding: "8px 0" }}>
        No recent audit events recorded.
      </div>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      {items.map((item, idx) => (
        <div key={item.id} style={{ display: "flex", gap: 12 }}>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
            <span
              style={{
                width: 9,
                height: 9,
                borderRadius: "50%",
                marginTop: 4,
                background: item.color || "var(--accent)",
                flexShrink: 0,
              }}
            />
            {idx < items.length - 1 && (
              <span
                style={{
                  width: 1,
                  flex: 1,
                  marginTop: 4,
                  background: "var(--border-hair)",
                }}
              />
            )}
          </div>
          <div style={{ paddingBottom: idx < items.length - 1 ? 14 : 2 }}>
            <div style={{ fontSize: 12.5, fontWeight: 500 }}>{item.title}</div>
            <div className="mono" style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 2 }}>
              {item.meta}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export function DropdownMenu({
  label,
  items,
}: {
  label: string;
  items: Array<
    | { type: "divider" }
    | {
        type?: "item";
        label: string;
        shortcut?: string;
        danger?: boolean;
        onClick: () => void;
      }
  >;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div ref={wrapRef} style={{ position: "relative" }}>
      <Button size="sm" onClick={() => setOpen((o) => !o)}>
        <span>{label}</span>
        <ChevronDown size={13} />
      </Button>
      {open && (
        <div
          className="surface modal-card"
          style={{
            position: "absolute",
            right: 0,
            top: "calc(100% + 6px)",
            width: 224,
            padding: 6,
            zIndex: 45,
            boxShadow: "0 14px 34px rgba(0,0,0,0.28)",
          }}
        >
          {items.map((entry, idx) =>
            entry.type === "divider" ? (
              <div
                key={`div-${idx}`}
                style={{
                  margin: "5px 4px",
                  borderTop: "1px solid var(--border-hair)",
                }}
              />
            ) : (
              <button
                key={entry.label}
                type="button"
                className={`nav-item ${entry.danger ? "status-err" : ""}`}
                onClick={() => {
                  setOpen(false);
                  entry.onClick();
                }}
              >
                <span style={{ flex: 1 }}>{entry.label}</span>
                {entry.shortcut && (
                  <span className="mono" style={{ fontSize: 10.5, color: "var(--text-muted)" }}>
                    {entry.shortcut}
                  </span>
                )}
              </button>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export function SlideOverDrawer({
  open,
  title,
  subtitle,
  onClose,
  footer,
  children,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  onClose: () => void;
  footer?: React.ReactNode;
  children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="backdrop"
      style={{ justifyContent: "flex-end" }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="surface drawer-panel"
        style={{
          width: "min(440px, 100vw)",
          height: "100vh",
          maxHeight: "100vh",
          borderRadius: 0,
          borderTop: 0,
          borderRight: 0,
          borderBottom: 0,
          display: "flex",
          flexDirection: "column",
        }}
      >
        <div
          style={{
            padding: "16px 20px",
            borderBottom: "1px solid var(--border-hair)",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 12,
          }}
        >
          <div>
            <div style={{ fontSize: 14, fontWeight: 600 }}>{title}</div>
            {subtitle && (
              <div className="mono" style={{ fontSize: 11.5, color: "var(--text-muted)", marginTop: 2 }}>
                {subtitle}
              </div>
            )}
          </div>
          <Chip onClick={onClose} aria-label="Close drawer">
            ✕
          </Chip>
        </div>

        <div style={{ flex: 1, overflowY: "auto", padding: 20 }}>{children}</div>

        {footer && (
          <div
            style={{
              padding: "14px 20px",
              borderTop: "1px solid var(--border-hair)",
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
            }}
          >
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}

export function Modal({
  open,
  title,
  onClose,
  maxWidth = 460,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  maxWidth?: number;
  children: React.ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return createPortal(
    <div
      className="backdrop"
      style={{ alignItems: "flex-start", justifyContent: "center", padding: "72px 16px 24px" }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="surface modal-card" style={{ width: "100%", maxWidth, padding: 20 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 14,
          }}
        >
          <span style={{ fontSize: 14, fontWeight: 600 }}>{title}</span>
          <Chip onClick={onClose}>Esc</Chip>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}

export function SkeletonCard({ lines = 3 }: { lines?: number }) {
  return (
    <SurfaceCard style={{ padding: 16 }}>
      <div className="skel" style={{ height: 14, width: "36%", marginBottom: 12 }} />
      {Array.from({ length: lines }).map((_, idx) => (
        <div
          key={idx}
          className="skel"
          style={{
            height: 11,
            width: idx === lines - 1 ? "72%" : "100%",
            marginBottom: idx < lines - 1 ? 8 : 0,
          }}
        />
      ))}
    </SurfaceCard>
  );
}

export function ToastBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return createPortal(
    <div
      className="surface modal-card"
      style={{
        position: "fixed",
        right: 20,
        bottom: 20,
        zIndex: 80,
        padding: "10px 14px",
        fontSize: 12.5,
        fontWeight: 500,
        boxShadow: "0 12px 30px rgba(0,0,0,0.3)",
      }}
    >
      {message}
    </div>,
    document.body,
  );
}
