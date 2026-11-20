export type Locale = "en" | "ar";

const LOCALE_STORAGE_KEY = "aibridge.locale.v1";

const DICTIONARY: Record<Locale, Record<string, string>> = {
  en: {
    "app.title": "AI Bridge",
    "app.subtitle": "Control Plane",
    "nav.group.Core": "Core",
    "nav.group.Automation": "Automation",
    "nav.group.Governance": "Governance",
    "nav.group.Platform": "Platform",
    "nav.overview": "Overview",
    "nav.devices": "Fleet & Device 360",
    "nav.terminal": "Live Terminal",
    "nav.desktop": "Live Desktop",
    "nav.tasks": "Task Explorer",
    "nav.workflows": "Workflows & Agents",
    "nav.fleetops": "Fleet Ops & Cron",
    "nav.security": "Security & Policy",
    "nav.audit": "Audit & Logs",
    "nav.connections": "Connections & MCP",
    "nav.settings": "Admin & Settings",
    "status.controlLink": "Control Link",
    "status.connected": "Connected",
    "status.connecting": "Connecting",
    "status.degraded": "Degraded",
    "status.lockdown": "Lockdown Active",
    "action.refresh": "Refresh telemetry",
    "action.signOut": "Sign Out",
    "action.quickJump": "Quick Jump (Ctrl+K)",
    "state.offlineTitle": "Control plane unreachable",
    "state.offlineDetail": "Live telemetry polling failed. Displayed data may be stale until connectivity recovers.",
    "state.deniedTitle": "Permission denied",
    "state.emptyTitle": "No data available",
    "state.degradedTitle": "Degraded state",
    "settings.language": "Language & Direction",
  },
  ar: {
    "app.title": "AI Bridge",
    "app.subtitle": "لوحة التحكم",
    "nav.group.Core": "الأساسي",
    "nav.group.Automation": "الأتمتة",
    "nav.group.Governance": "الحوكمة",
    "nav.group.Platform": "المنصة",
    "nav.overview": "نظرة عامة",
    "nav.devices": "الأسطول والأجهزة",
    "nav.terminal": "الطرفية المباشرة",
    "nav.desktop": "سطح المكتب المباشر",
    "nav.tasks": "مستكشف المهام",
    "nav.workflows": "سير العمل والوكلاء",
    "nav.fleetops": "عمليات الأسطول",
    "nav.security": "الأمان والسياسات",
    "nav.audit": "التدقيق والسجلات",
    "nav.connections": "الاتصالات و MCP",
    "nav.settings": "الإدارة والإعدادات",
    "status.controlLink": "رابط التحكم",
    "status.connected": "متصل",
    "status.connecting": "جارٍ الاتصال",
    "status.degraded": "متدهور",
    "status.lockdown": "إيقاف طارئ نشط",
    "action.refresh": "تحديث القياسات",
    "action.signOut": "تسجيل الخروج",
    "action.quickJump": "انتقال سريع (Ctrl+K)",
    "state.offlineTitle": "تعذر الوصول إلى لوحة التحكم",
    "state.offlineDetail": "فشل تحديث البيانات المباشرة. قد تكون البيانات المعروضة قديمة حتى يعود الاتصال.",
    "state.deniedTitle": "تم رفض الإذن",
    "state.emptyTitle": "لا توجد بيانات",
    "state.degradedTitle": "حالة متدهورة",
    "settings.language": "اللغة والاتجاه",
  },
};

export function loadStoredLocale(): Locale {
  try {
    const raw = localStorage.getItem(LOCALE_STORAGE_KEY);
    return raw === "ar" ? "ar" : "en";
  } catch {
    return "en";
  }
}

export function saveStoredLocale(locale: Locale): void {
  try {
    localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Ignore storage errors
  }
}

export function applyLocaleDocument(locale: Locale): void {
  document.documentElement.lang = locale;
  document.documentElement.dir = locale === "ar" ? "rtl" : "ltr";
}

export function t(locale: Locale, key: string, fallback?: string): string {
  return DICTIONARY[locale]?.[key] ?? DICTIONARY.en[key] ?? fallback ?? key;
}
