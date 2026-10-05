// Адрес API и режим работы. В реальном режиме по умолчанию используется /api на том же
// домене (в разработке его проксирует Vite) — так httpOnly-cookie сессии работает без CORS.
export const API_URL = (import.meta.env.VITE_API_URL as string | undefined)?.replace(/\/$/, '') ?? '/api'
export const USE_MOCK = import.meta.env.VITE_USE_MOCK === 'true'
