export function ariaIdToken(value: string): string {
  const token = Array.from(value, (character) =>
    character.codePointAt(0)!.toString(16),
  ).join("-");

  return token || "empty";
}
