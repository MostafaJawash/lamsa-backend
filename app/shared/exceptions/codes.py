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

    # Catalog: lookups
    CATEGORY_NOT_FOUND = "CATEGORY_NOT_FOUND"
    BRAND_NOT_FOUND = "BRAND_NOT_FOUND"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    VARIANT_NOT_FOUND = "VARIANT_NOT_FOUND"
    IMAGE_NOT_FOUND = "IMAGE_NOT_FOUND"
    ATTRIBUTE_NOT_FOUND = "ATTRIBUTE_NOT_FOUND"

    # Catalog: uniqueness and structure
    SLUG_TAKEN = "SLUG_TAKEN"
    SKU_TAKEN = "SKU_TAKEN"
    CATEGORY_NOT_EMPTY = "CATEGORY_NOT_EMPTY"
    CATEGORY_DEPTH_EXCEEDED = "CATEGORY_DEPTH_EXCEEDED"
    BRAND_HAS_PRODUCTS = "BRAND_HAS_PRODUCTS"

    # Catalog: definitions
    ATTRIBUTE_KEY_TAKEN = "ATTRIBUTE_KEY_TAKEN"
    ATTRIBUTE_IN_USE = "ATTRIBUTE_IN_USE"
    OPTION_IN_USE = "OPTION_IN_USE"
    VARIANT_OPTION_IN_USE = "VARIANT_OPTION_IN_USE"

    # Catalog: product data
    UNKNOWN_ATTRIBUTE = "UNKNOWN_ATTRIBUTE"
    INVALID_ATTRIBUTE_VALUE = "INVALID_ATTRIBUTE_VALUE"
    MISSING_REQUIRED_ATTRIBUTE = "MISSING_REQUIRED_ATTRIBUTE"
    INVALID_VARIANT_OPTIONS = "INVALID_VARIANT_OPTIONS"
    DUPLICATE_VARIANT = "DUPLICATE_VARIANT"
    PRODUCT_TYPE_IMMUTABLE = "PRODUCT_TYPE_IMMUTABLE"
    INVALID_PRODUCT_FOR_TYPE = "INVALID_PRODUCT_FOR_TYPE"
    PRODUCT_NOT_PUBLISHABLE = "PRODUCT_NOT_PUBLISHABLE"
    PRODUCT_HAS_ORDERS = "PRODUCT_HAS_ORDERS"
    PRODUCT_IN_BUNDLE = "PRODUCT_IN_BUNDLE"
    VARIANT_HAS_ORDERS = "VARIANT_HAS_ORDERS"
    VARIANT_IN_BUNDLE = "VARIANT_IN_BUNDLE"
    INVALID_BUNDLE_ITEM = "INVALID_BUNDLE_ITEM"
    STOCK_NEGATIVE = "STOCK_NEGATIVE"

    # Catalog: images and uploads
    IMAGE_LIMIT_REACHED = "IMAGE_LIMIT_REACHED"
    IMAGE_ORDER_MISMATCH = "IMAGE_ORDER_MISMATCH"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_IMAGE = "INVALID_IMAGE"

    # Catalog: listing
    INVALID_FILTER = "INVALID_FILTER"

    # Cart and favorites
    CART_ITEM_NOT_FOUND = "CART_ITEM_NOT_FOUND"
    VARIANT_REQUIRED = "VARIANT_REQUIRED"
    VARIANT_NOT_ALLOWED = "VARIANT_NOT_ALLOWED"
    QUANTITY_LIMIT_EXCEEDED = "QUANTITY_LIMIT_EXCEEDED"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    CART_LIMIT_REACHED = "CART_LIMIT_REACHED"
    FAVORITES_LIMIT_REACHED = "FAVORITES_LIMIT_REACHED"


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
    ErrorCode.CATEGORY_NOT_FOUND: "القسم غير موجود.",
    ErrorCode.BRAND_NOT_FOUND: "الماركة غير موجودة.",
    ErrorCode.PRODUCT_NOT_FOUND: "المنتج غير موجود.",
    ErrorCode.VARIANT_NOT_FOUND: "هذا الخيار من المنتج غير موجود.",
    ErrorCode.IMAGE_NOT_FOUND: "الصورة غير موجودة.",
    ErrorCode.ATTRIBUTE_NOT_FOUND: "الخاصية غير موجودة.",
    ErrorCode.SLUG_TAKEN: "هذا الرابط المختصر مستخدم. اختر رابطاً آخر.",
    ErrorCode.SKU_TAKEN: "رمز المنتج (SKU) مستخدم لمنتج آخر.",
    ErrorCode.CATEGORY_NOT_EMPTY: "لا يمكن حذف قسم فيه أقسام فرعية أو منتجات. أوقفه بدلاً من ذلك.",
    ErrorCode.CATEGORY_DEPTH_EXCEEDED: "الأقسام مستويان فقط: قسم رئيسي وأقسام فرعية تحته.",
    ErrorCode.BRAND_HAS_PRODUCTS: "لا يمكن حذف ماركة لها منتجات. أوقفها بدلاً من ذلك.",
    ErrorCode.ATTRIBUTE_KEY_TAKEN: "هذا المفتاح مستخدم في هذا القسم.",
    ErrorCode.ATTRIBUTE_IN_USE: "هذه الخاصية مستخدمة في منتجات حالياً.",
    ErrorCode.OPTION_IN_USE: "هذه القيمة مستخدمة في منتجات حالياً.",
    ErrorCode.VARIANT_OPTION_IN_USE: "هذا الخيار مستخدم في منتجات حالياً.",
    ErrorCode.UNKNOWN_ATTRIBUTE: "هذه الخاصية غير معرّفة في قسم المنتج.",
    ErrorCode.INVALID_ATTRIBUTE_VALUE: "قيمة الخاصية غير صحيحة.",
    ErrorCode.MISSING_REQUIRED_ATTRIBUTE: "بعض الخصائص المطلوبة ناقصة.",
    ErrorCode.INVALID_VARIANT_OPTIONS: "خيارات المنتج (اللون أو الحجم...) غير صحيحة.",
    ErrorCode.DUPLICATE_VARIANT: "يوجد خيار بنفس المواصفات لهذا المنتج.",
    ErrorCode.PRODUCT_TYPE_IMMUTABLE: "لا يمكن تغيير نوع المنتج بعد إنشائه.",
    ErrorCode.INVALID_PRODUCT_FOR_TYPE: "هذا الحقل غير مسموح لهذا النوع من المنتجات.",
    ErrorCode.PRODUCT_NOT_PUBLISHABLE: "المنتج غير جاهز للنشر بعد.",
    ErrorCode.PRODUCT_HAS_ORDERS: "لا يمكن حذف منتج موجود في طلبات. أوقفه بدلاً من ذلك.",
    ErrorCode.PRODUCT_IN_BUNDLE: "لا يمكن حذف منتج موجود داخل بوكس هدايا.",
    ErrorCode.VARIANT_HAS_ORDERS: "لا يمكن حذف خيار موجود في طلبات. أوقفه بدلاً من ذلك.",
    ErrorCode.VARIANT_IN_BUNDLE: "لا يمكن حذف خيار موجود داخل بوكس هدايا.",
    ErrorCode.INVALID_BUNDLE_ITEM: "محتويات البوكس غير صحيحة.",
    ErrorCode.STOCK_NEGATIVE: "الكمية في المخزون لا يمكن أن تصبح أقل من صفر.",
    ErrorCode.IMAGE_LIMIT_REACHED: "وصلت للحد الأقصى من الصور لهذا المنتج.",
    ErrorCode.IMAGE_ORDER_MISMATCH: "ترتيب الصور يجب أن يحتوي كل صور المنتج مرة واحدة.",
    ErrorCode.FILE_TOO_LARGE: "حجم الملف أكبر من المسموح.",
    ErrorCode.INVALID_IMAGE: "الملف ليس صورة صالحة (JPEG أو PNG أو WEBP).",
    ErrorCode.INVALID_FILTER: "خيارات التصفية غير صحيحة.",
    ErrorCode.CART_ITEM_NOT_FOUND: "هذا المنتج غير موجود في سلتك.",
    ErrorCode.VARIANT_REQUIRED: "اختر اللون أو الحجم أولاً.",
    ErrorCode.VARIANT_NOT_ALLOWED: "هذا المنتج ليس له خيارات.",
    ErrorCode.QUANTITY_LIMIT_EXCEEDED: "تجاوزت الكمية المسموحة لهذا المنتج.",
    ErrorCode.INSUFFICIENT_STOCK: "الكمية المطلوبة غير متوفرة حالياً.",
    ErrorCode.OUT_OF_STOCK: "هذا المنتج نفد من المخزون.",
    ErrorCode.CART_LIMIT_REACHED: "سلتك ممتلئة. احذف بعض المنتجات أولاً.",
    ErrorCode.FAVORITES_LIMIT_REACHED: "وصلت للحد الأقصى من المفضلة.",
}


class CartIssue(StrEnum):
    """Problems of a cart line, reported on reads (not errors; the line is still returned)."""

    UNAVAILABLE = "UNAVAILABLE"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
    QUANTITY_LIMIT_EXCEEDED = "QUANTITY_LIMIT_EXCEEDED"


class CartNotice(StrEnum):
    """What the current request changed in the cart (not errors)."""

    QUANTITY_CAPPED = "QUANTITY_CAPPED"
    LINES_MERGED = "LINES_MERGED"
    LINE_REMOVED = "LINE_REMOVED"


ISSUE_MESSAGES: dict[CartIssue, str] = {
    CartIssue.UNAVAILABLE: "هذا المنتج لم يعد متوفراً.",
    CartIssue.VARIANT_UNAVAILABLE: "هذا الخيار (اللون أو الحجم) لم يعد متوفراً.",
    CartIssue.OUT_OF_STOCK: "نفد من المخزون.",
    CartIssue.INSUFFICIENT_STOCK: "الكمية المتوفرة أقل من المطلوب.",
    CartIssue.QUANTITY_LIMIT_EXCEEDED: "الكمية أكبر من المسموح لهذا المنتج.",
}

NOTICE_MESSAGES: dict[CartNotice, str] = {
    CartNotice.QUANTITY_CAPPED: "تم تعديل الكمية حسب المتوفر.",
    CartNotice.LINES_MERGED: "تم دمج منتجين متطابقين في سطر واحد.",
    CartNotice.LINE_REMOVED: "تم حذف منتج لم يعد متوفراً.",
}
