import {
  Bot,
  Braces,
  Box,
  CircleCheck,
  CircleDashed,
  Ban,
  Database,
  FileCode,
  FileDiff,
  FileText,
  Hourglass,
  Image,
  Layers,
  LoaderCircle,
  Search,
  ShieldCheck,
  Sparkles,
  Star,
  Table,
  TriangleAlert,
  UserCheck,
  Waypoints,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import type { Status } from "../types";

export interface KindStyle {
  label: string;
  color: string;
  icon: LucideIcon;
}

export const SPAN_KINDS: Record<string, KindStyle> = {
  agent: { label: "Agent", color: "var(--k-agent)", icon: Bot },
  llm: { label: "Model call", color: "var(--k-llm)", icon: Sparkles },
  tool: { label: "Tool", color: "var(--k-tool)", icon: Wrench },
  retrieval: { label: "Retrieval", color: "var(--k-retrieval)", icon: Search },
  delegation: { label: "Delegation", color: "var(--k-delegation)", icon: Waypoints },
  approval: { label: "Approval", color: "var(--k-approval)", icon: UserCheck },
  guardrail: { label: "Guardrail", color: "var(--k-guardrail)", icon: ShieldCheck },
  embedding: { label: "Embedding", color: "var(--k-embedding)", icon: Layers },
  step: { label: "Step", color: "var(--k-step)", icon: Box },
};

export function spanKind(kind: string): KindStyle {
  return SPAN_KINDS[kind] ?? SPAN_KINDS.step;
}

export const ARTIFACT_ICONS: Record<string, LucideIcon> = {
  answer: Star,
  markdown: FileText,
  report: FileText,
  text: FileText,
  code: FileCode,
  diff: FileDiff,
  json: Braces,
  document: Database,
  file: FileText,
  image: Image,
  table: Table,
};

export function artifactIcon(kind: string): LucideIcon {
  return ARTIFACT_ICONS[kind] ?? FileText;
}

export const STATUS: Record<Status, { label: string; color: string; icon: LucideIcon }> = {
  running: { label: "Running", color: "var(--s-running)", icon: LoaderCircle },
  ok: { label: "Succeeded", color: "var(--s-ok)", icon: CircleCheck },
  error: { label: "Failed", color: "var(--s-error)", icon: TriangleAlert },
  waiting: { label: "Waiting", color: "var(--s-waiting)", icon: Hourglass },
  cancelled: { label: "Cancelled", color: "var(--s-muted)", icon: Ban },
  skipped: { label: "Skipped", color: "var(--s-muted)", icon: CircleDashed },
};

export function statusStyle(s: string | undefined) {
  return STATUS[(s as Status) ?? "ok"] ?? STATUS.ok;
}

export const RELATIONS: Record<string, { label: string; inverse: string; tone: "support" | "contradict" | "neutral" }> = {
  supported_by: { label: "Supported by", inverse: "Supports", tone: "support" },
  cites: { label: "Cites", inverse: "Cited by", tone: "support" },
  contradicted_by: { label: "Contradicted by", inverse: "Contradicts", tone: "contradict" },
  derived_from: { label: "Derived from", inverse: "Used to derive", tone: "neutral" },
  used: { label: "Used", inverse: "Used by", tone: "neutral" },
  tested_by: { label: "Tested by", inverse: "Tests", tone: "support" },
  revises: { label: "Revises", inverse: "Revised by", tone: "neutral" },
};

export function relation(r: string) {
  return RELATIONS[r] ?? { label: r.replace(/_/g, " "), inverse: `${r.replace(/_/g, " ")} (inverse)`, tone: "neutral" as const };
}
