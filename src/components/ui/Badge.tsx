import type { ReactNode } from "react";

type BadgeVariant = "success" | "error" | "warning" | "info" | "muted" | "primary";

const VARIANT_STYLES: Record<BadgeVariant, string> = {
  success: "bg-success/12 text-success",
  error: "bg-destructive/10 text-destructive",
  warning: "bg-warning/12 text-warning",
  info: "bg-primary/10 text-primary",
  muted: "bg-secondary text-muted-foreground",
  primary: "bg-primary/10 text-primary",
};

interface BadgeProps {
  variant?: BadgeVariant;
  children: ReactNode;
  className?: string;
}

export default function Badge({ variant = "muted", children, className = "" }: BadgeProps) {
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold ${VARIANT_STYLES[variant]} ${className}`}>
      {children}
    </span>
  );
}
