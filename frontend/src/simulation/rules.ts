/** Task location rules matching backend/app/sim/rules.py: existing, non-charger, distinct, and compatible with TaskType. (Quy tắc vị trí tác vụ khớp backend/app/sim/rules.py: phải tồn tại, không phải trạm sạc, khác nhau và phù hợp TaskType.) */
import type { LayoutLocation } from "../layout/types";

export const TASK_RULES: Record<string, [Set<string>, Set<string>]> = {
  PICK: [new Set(["SHELF"]), new Set(["PACKING", "SORTING", "OUTBOUND"])],
  REPLENISH: [new Set(["INBOUND"]), new Set(["SHELF"])],
  TRANSPORT: [new Set(["PACKING", "SORTING", "INBOUND", "OUTBOUND"]), new Set(["PACKING", "SORTING", "INBOUND", "OUTBOUND"])],
  RETURN: [new Set(["PACKING", "SORTING", "OUTBOUND"]), new Set(["SHELF"])],
};

/** Return an error message; null means valid. (Trả về thông báo lỗi; null nghĩa là hợp lệ.) */
export function taskError(loc: Record<string, LayoutLocation>, type: string, source: string, destination: string): string | null {
  const rule = TASK_RULES[type];
  if (!rule) return `unknown task type ${type}`;
  const s = loc[source], d = loc[destination];
  if (!s) return `unknown source location ${source}`;
  if (!d) return `unknown destination location ${destination}`;
  if (source === destination) return "source and destination must differ";
  if (s.kind === "CHARGING" || d.kind === "CHARGING") return "charging stations cannot be task locations";
  if (!rule[0].has(s.kind)) return `${type} source must be one of ${[...rule[0]].sort().join(", ")}, got ${s.kind}`;
  if (!rule[1].has(d.kind)) return `${type} destination must be one of ${[...rule[1]].sort().join(", ")}, got ${d.kind}`;
  return null;
}
