from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error codes returned in {"error": {"code": ...}}. Never rename one."""

    # Generic
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    HTTP_ERROR = "HTTP_ERROR"

    # Auth
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    TOKEN_INVALID = "TOKEN_INVALID"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    TOKEN_REUSED = "TOKEN_REUSED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    USER_NOT_FOUND = "USER_NOT_FOUND"
    ADMIN_ONLY = "ADMIN_ONLY"
    PASSWORD_CHANGE_REQUIRED = "PASSWORD_CHANGE_REQUIRED"
    GOOGLE_TOKEN_INVALID = "GOOGLE_TOKEN_INVALID"
    ALREADY_REGISTERED = "ALREADY_REGISTERED"

    # Account data
    INVALID_PHONE = "INVALID_PHONE"
    PHONE_TAKEN = "PHONE_TAKEN"
    WEAK_PASSWORD = "WEAK_PASSWORD"
    SAME_PASSWORD = "SAME_PASSWORD"
    NOT_A_PASSWORD_ACCOUNT = "NOT_A_PASSWORD_ACCOUNT"
    CURRENT_PASSWORD_REQUIRED = "CURRENT_PASSWORD_REQUIRED"

    # Addresses
    ADDRESS_NOT_FOUND = "ADDRESS_NOT_FOUND"
    ADDRESS_LIMIT_REACHED = "ADDRESS_LIMIT_REACHED"

    # Admin
    CANNOT_MODIFY_SELF = "CANNOT_MODIFY_SELF"


# Default short Arabic message per code, shown to the user by the app.
MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.VALIDATION_ERROR: "البيانات المرسلة غير صحيحة.",
    ErrorCode.INTERNAL_ERROR: "حدث خطأ غير متوقع. حاول مرة أخرى لاحقاً.",
    ErrorCode.NOT_FOUND: "الصفحة المطلوبة غير موجودة.",
    ErrorCode.METHOD_NOT_ALLOWED: "هذه العملية غير مسموحة.",
    ErrorCode.HTTP_ERROR: "تعذر تنفيذ الطلب.",
    ErrorCode.NOT_AUTHENTICATED: "يجب تسجيل الدخول أولاً.",
    ErrorCode.TOKEN_INVALID: "جلسة الدخول غير صالحة. سجّل الدخول من جديد.",
    ErrorCode.TOKEN_EXPIRED: "انتهت صلاحية الجلسة.",
    ErrorCode.TOKEN_REUSED: "تم إنهاء الجلسة لأسباب أمنية. سجّل الدخول من جديد.",
    ErrorCode.INVALID_CREDENTIALS: "رقم الهاتف أو كلمة المرور غير صحيحة.",
    ErrorCode.TOO_MANY_ATTEMPTS: "محاولات كثيرة. حاول مرة أخرى بعد قليل.",
    ErrorCode.ACCOUNT_DISABLED: "هذا الحساب موقوف. تواصل مع المتجر.",
    ErrorCode.USER_NOT_FOUND: "المستخدم غير موجود.",
    ErrorCode.ADMIN_ONLY: "هذه العملية متاحة للإدارة فقط.",
    ErrorCode.PASSWORD_CHANGE_REQUIRED: "يجب تغيير كلمة المرور المؤقتة أولاً.",
    ErrorCode.GOOGLE_TOKEN_INVALID: "تعذر التحقق من حساب Google. حاول مرة أخرى.",
    ErrorCode.ALREADY_REGISTERED: "حساب Google هذا مسجل مسبقاً. سجّل الدخول.",
    ErrorCode.INVALID_PHONE: "رقم الهاتف غير صحيح.",
    ErrorCode.PHONE_TAKEN: "رقم الهاتف مستخدم في حساب آخر.",
    ErrorCode.WEAK_PASSWORD: "كلمة المرور ضعيفة.",
    ErrorCode.SAME_PASSWORD: "كلمة المرور الجديدة يجب أن تختلف عن الحالية.",
    ErrorCode.NOT_A_PASSWORD_ACCOUNT: "هذا الحساب يستخدم تسجيل الدخول عبر Google ولا يملك كلمة مرور.",
    ErrorCode.CURRENT_PASSWORD_REQUIRED: "أدخل كلمة المرور الحالية للتأكيد.",
    ErrorCode.ADDRESS_NOT_FOUND: "العنوان غير موجود.",
    ErrorCode.ADDRESS_LIMIT_REACHED: "وصلت للحد الأقصى من العناوين (10).",
    ErrorCode.CANNOT_MODIFY_SELF: "لا يمكنك إيقاف حسابك أو إزالة صلاحية الإدارة عن نفسك.",
}
