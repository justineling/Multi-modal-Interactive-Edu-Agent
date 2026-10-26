/**
 * 极简 className 拼接器。
 *
 * 不引 clsx/tailwind-merge：本项目类名都是静态字面量 + 少量条件，
 * 一个 filter+join 足够，省一个依赖。
 */
export type ClassValue = string | false | null | undefined;

export function cx(...values: ClassValue[]): string {
  return values.filter((value): value is string => typeof value === "string" && value.length > 0).join(" ");
}
