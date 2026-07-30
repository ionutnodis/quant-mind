// Currency symbol for money display, driven by the API's base_currency
// (FX-aware valuation, 2026-07-27). Deliberately tiny and separate from
// api.ts so adopting it is page-scoped — no other page's contract moves.
// Unknown codes fall back to the ISO code + space ("SEK 123.45"): honest,
// never a wrong symbol.
export function currencySymbol(code: string): string {
  switch (code) {
    case "GBP":
      return "£";
    case "USD":
      return "$";
    case "EUR":
      return "€";
    default:
      return `${code} `;
  }
}
