import { delay, http, HttpResponse } from 'msw'
import * as engine from './engine'
import * as auth from './auth'
import { API_URL } from '../client'

// Перехватываем ровно те же URL, что будет обслуживать FastAPI
const url = (path: string) => `${API_URL}${path}`
const notFound = () => HttpResponse.json({ detail: 'Not found' }, { status: 404 })
// Формат ошибок как у FastAPI-бэкенда: { error, message }
const fail = (status: number, error: string, message: string) => HttpResponse.json({ error, message }, { status })
const unauthorized = () => fail(401, 'unauthorized', 'Нужно войти в систему')
const forbidden = () => fail(403, 'forbidden', 'Недостаточно прав')
const completionReplay = new Map<string, { target: string; result: NonNullable<ReturnType<typeof engine.completeActivity>> }>()
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

// Права как в backend/api/auth.py: HR видит всех, сотрудник — только себя
async function guard(request: Request, opts: { hr?: boolean; employeeId?: string } = {}) {
  const acc = await auth.accountByRequest(request)
  if (!acc) return unauthorized()
  if (opts.hr && acc.role !== 'hr') return forbidden()
  if (opts.employeeId && acc.role !== 'hr' && acc.employeeId !== opts.employeeId) return forbidden()
  return null
}

export const handlers = [
  // ---------- Авторизация ----------
  http.post(url('/auth/login'), async ({ request }) => {
    await delay(500)
    const { email, password } = (await request.json()) as { email: string; password: string }
    const res = await auth.login(email ?? '', password ?? '')
    return res ? HttpResponse.json(res) : fail(401, 'invalid_credentials', 'Неверная почта или пароль')
  }),

  http.post(url('/auth/register'), async ({ request }) => {
    await delay(600)
    const body = (await request.json()) as Parameters<typeof auth.register>[0]
    if (!body?.name?.trim() || !body?.email?.includes('@') || (body?.password ?? '').length < 6) {
      return fail(422, 'invalid_request', 'Проверьте имя, почту и пароль (минимум 6 символов)')
    }
    const res = await auth.register(body)
    return 'error' in res ? fail(409, 'conflict', res.error!) : HttpResponse.json(res.account)
  }),

  http.get(url('/auth/me'), async ({ request }) => {
    const acc = await auth.accountByRequest(request)
    if (!acc) return unauthorized()
    return HttpResponse.json(auth.publicAccount(acc))
  }),

  http.post(url('/auth/logout'), async ({ request }) => {
    await auth.logout(request)
    return HttpResponse.json({ status: 'signed_out' })
  }),

  http.post(url('/auth/consent'), async ({ request }) => {
    const acc = await auth.accountByRequest(request)
    if (!acc) return unauthorized()
    return HttpResponse.json(await auth.setConsent(acc, true))
  }),

  http.delete(url('/auth/consent'), async ({ request }) => {
    const acc = await auth.accountByRequest(request)
    if (!acc) return unauthorized()
    return HttpResponse.json(await auth.setConsent(acc, false))
  }),

  http.post(url('/auth/password'), async ({ request }) => {
    const acc = await auth.accountByRequest(request)
    if (!acc) return unauthorized()
    const body = (await request.json()) as { current_password: string; new_password: string }
    const result = await auth.changePassword(acc, body.current_password ?? '', body.new_password ?? '')
    if (result === 'invalid_password') return fail(400, result, 'Текущий пароль указан неверно')
    if (result === 'weak_password') return fail(422, result, 'Пароль должен быть не короче 10 символов и содержать цифры')
    return HttpResponse.json(result)
  }),

  http.post(url('/employees/:id/promote'), async ({ params, request }) => {
    const denied = await guard(request, { hr: true }); if (denied) return denied
    const body = (await request.json().catch(() => ({}))) as { force?: boolean; expected_grade?: string }
    const current = engine.getProfile(params.id as string)
    if (current && body.expected_grade && current.grade !== body.expected_grade) {
      return fail(409, 'grade_changed', `Грейд уже изменён на «${current.grade}» — обновите страницу`)
    }
    const result = engine.promote(params.id as string, !!body.force)
    if (result === 'requirements_not_met') return fail(422, result, 'Обязательные требования следующего грейда не выполнены')
    return result ? HttpResponse.json(result) : notFound()
  }),

  // Публичный справочник для формы регистрации: выбрать свой профиль
  http.get(url('/auth/directory'), async () => {
    await delay(200)
    return HttpResponse.json(engine.listEmployees().map((e) => ({ id: e.id, name: e.name, role: e.role, department: e.department })))
  }),

  http.get(url('/employees'), async ({ request }) => {
    const denied = await guard(request, { hr: true }); if (denied) return denied
    await delay(300)
    return HttpResponse.json(engine.listEmployees())
  }),

  http.get(url('/employees/:id/profile'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(350)
    const p = engine.getProfile(params.id as string)
    return p ? HttpResponse.json(p) : notFound()
  }),

  http.get(url('/employees/:id/recommendations'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(700) // имитируем расчёт рекомендаций и объяснений
    return HttpResponse.json(engine.getRecommendations(params.id as string))
  }),

  http.get(url('/employees/:id/comparison-options'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(200)
    const options = engine.getComparisonOptions(params.id as string)
    return options ? HttpResponse.json(options) : notFound()
  }),

  http.get(url('/employees/:id/simulate/:eventId'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(250)
    const result = engine.simulateActivity(params.id as string, params.eventId as string)
    return result ? HttpResponse.json(result) : notFound()
  }),

  http.get(url('/employees/:id/compare'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(250)
    const search = new URL(request.url).searchParams
    const result = engine.compareActivities(
      params.id as string,
      search.get('first_event_id') ?? '',
      search.get('second_event_id') ?? '',
    )
    return result ? HttpResponse.json(result) : notFound()
  }),

  http.get(url('/employees/:id/history'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(250)
    return HttpResponse.json(engine.getHistory(params.id as string))
  }),

  http.post(url('/employees/:id/activities/:aid/complete'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(600)
    const key = request.headers.get('Idempotency-Key') ?? ''
    if (!UUID.test(key)) return fail(422, 'invalid_idempotency_key', 'Idempotency-Key должен быть UUID')
    const account = await auth.accountByRequest(request)
    const scopedKey = `${account!.accountId}:${key}`
    const target = `${params.id}/${params.aid}`
    const replay = completionReplay.get(scopedKey)
    if (replay && replay.target !== target) {
      return fail(409, 'idempotency_conflict', 'Idempotency-Key уже использован для другой активности')
    }
    if (replay) return HttpResponse.json(replay.result, { headers: { 'Idempotency-Replayed': 'true' } })
    const r = engine.completeActivity(params.id as string, params.aid as string)
    if (!r) return notFound()
    completionReplay.set(scopedKey, { target, result: r })
    return HttpResponse.json(r, { headers: { 'Idempotency-Replayed': 'false' } })
  }),

  http.post(url('/employees/:id/activities/:aid/:action'), async ({ params, request }) => {
    const denied = await guard(request, { employeeId: params.id as string }); if (denied) return denied
    await delay(300)
    const action = params.action as string
    if (action !== 'skip' && action !== 'decline') return notFound()
    const ok = engine.rejectActivity(params.id as string, params.aid as string, action === 'skip' ? 'skipped' : 'declined')
    return ok ? HttpResponse.json({ ok: true }) : notFound()
  }),

  http.get(url('/hr/stats'), async ({ request }) => {
    const denied = await guard(request, { hr: true }); if (denied) return denied
    await delay(400)
    return HttpResponse.json(engine.getHrStats())
  }),
]
