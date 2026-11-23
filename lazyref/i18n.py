"""Language preferences and translations for built-in messages."""

from contextlib import contextmanager
from contextvars import ContextVar

DEFAULT_LANGUAGE = "uk"
LANGUAGES = {"uk": "Українська", "en": "English"}
_message_language = ContextVar("message_language", default=DEFAULT_LANGUAGE)


def translate(source, language=None, **fields):
    if language is None:
        language = _message_language.get()
    message = ENGLISH.get(source, source) if language == "en" else source
    return message.format(**fields) if fields else message


@contextmanager
def message_language(language):
    """Set the language for progress messages in the current worker."""
    token = _message_language.set(language)
    try:
        yield
    finally:
        _message_language.reset(token)


ENGLISH = {
    # Quick start
    "Інструкція LazyRef": "LazyRef quick start",
    "Як користуватися LazyRef": "How to use LazyRef",
    "Опиши позу, ракурс і важливі деталі. Потім обери режим пошуку.":
        "Describe the pose, camera angle, and important details. Then choose a search mode.",
    "Агент Codex · пошук зображень": "Codex agent · image search",
    "Вкажи кількість референсів і натисни «Знайти референси». Наприклад, введи «Людина сидить навпочіпки, видно все тіло, ракурс збоку».":
        "Choose how many references you want and click “Find references.” Try “A person squatting, full body, side view.”",
    "ШІ-зразок → реальні фото": "AI pose guide → real photos",
    "Натисни «Створити ШІ-варіанти» й обери зразок із потрібною позою. LazyRef шукатиме за ним справжні фотографії.":
        "Click “Generate AI variants” and choose the closest pose. LazyRef will search for real photos with that pose.",
    "Зображення → референси": "Image → references",
    "Обери фото з комп’ютера або галереї. Уточни, що знайти за зразком, і натисни «Знайти референси».":
        "Choose a photo from your computer or the galleries. Describe what to look for and click “Find references.”",
    "Результати й галереї": "Results and galleries",
    "Кнопка «Відкрити папку» покаже файли поточної добірки. У «Галереях» можна переглянути збережені фото й ШІ-зразки.":
        "Click “Open folder” to see the current results on disk. Open “Galleries” to browse saved photos and AI guides.",
    "Для пошуку й генерації потрібен Codex CLI. За потреби вкажи шлях до codex.exe в налаштуваннях.":
        "Search and generation use Codex CLI. If needed, enter the path to codex.exe in Settings.",
    "Не показувати більше": "Don't show again",
    "Зрозуміло": "Got it",
    "Закрити": "Close",

    # Settings
    "Налаштування LazyRef": "LazyRef settings",
    "Мова інтерфейсу": "Interface language",
    "Відкрити інструкцію": "Open quick start guide",
    "Пошук і підключення": "Search and connection",
    "Посилений відсів повторів": "Enhanced duplicate filtering",
    "Додатково порівнює перевірені фото з помірним обрізанням або водяним знаком. Локально, без окремих викликів Codex.":
        "Checks for near-duplicates among reviewed photos, including moderate crops or watermarks. Runs locally.",
    "Шукати тільки високоякісні фото": "Require high-resolution photos",
    "Довша сторона ≥ 1200 px, коротша ≥ 800 px. Без збільшення маленьких фото.":
        "Minimum dimensions are 1200 px on the long side and 800 px on the short side.",
    "Це поріг роздільності, а не гарантія різкості.": "Sharpness depends on the source photo.",
    "Широкий пошук · новий": "Wide search · new",
    "Попередній пошук · 0.3.4": "Legacy search · 0.3.4",
    "Алгоритм пошуку": "Search algorithm",
    "Алгоритм агента": "Agent algorithm",
    "Новий: максимум кандидатів": "Wide search candidate limit",
    "Новий: нових детальних оцінок": "Wide search review limit",
    "Повторно використовувати попередні результати": "Reuse previous results",
    "Фото й оцінки для того самого запиту зберігаються локально. Для незалежного порівняння режимів вимкни кеш.":
        "Photos and assessments for the same request are cached locally. Turn off reuse to compare search modes independently.",
    "8 — короткий тест": "8 — short test",
    "24 — швидкий пошук": "24 — quick search",
    "48 — середня глибина": "48 — medium depth",
    "72 — рекомендовано": "72 — recommended",
    "96 — більше варіантів": "96 — more options",
    "120 — найбільша добірка": "120 — largest selection",
    "Попередній алгоритм: перевірок": "Legacy search review limit",
    "Максимум циклів агента": "Maximum agent rounds",
    "Знайти codex.exe автоматично": "Find codex.exe automatically",
    "Модель пошуку": "Search model",
    "Модель генерації ШІ-зразків": "AI guide generation model",
    "Скріншот, якщо фото не завантажується": "Try a screenshot when a photo fails to download",
    "Дозволяти художню натуру дорослих (18+)": "Allow artistic nudity (adults only)",
    "Дозволяє нееротичні анатомічні референси дорослих в обох режимах перевірки.":
        "Allows non-erotic adult anatomy references with either source verification setting.",
    "Новий алгоритм: збір кандидатів → короткий відбір мініатюр → перевірка джерел і детальний огляд найкращих. "
    "Ліміти — максимуми, а не гарантована кількість фото. Попередній алгоритм доступний для порівняння й повернення до старого пошуку.":
        "Wide search gathers candidates, screens thumbnails, and reviews the best photos and their sources in detail. "
        "The limits cap how many candidates it can process. Choose Legacy search to use the previous algorithm.",
    "Перевірка джерела ON/OFF перемикається в головному вікні. OFF оцінює відповідність пози без перевірки походження. "
    "Скріншот зі сторінки доступний лише при ON. Навіть ON не гарантує автентичність.":
        "Toggle source verification in the main window. With verification OFF, LazyRef reviews the pose only. "
        "Turn verification ON to check photo origins and allow page screenshots. Treat the authenticity assessment as an estimate.",
    "Зберегти": "Save",
    "Скасувати": "Cancel",
    "Некоректні налаштування: {fields}. Застосовано типові значення для цих полів. Перед збереженням "
    "початковий файл буде збережено окремою резервною копією.":
        "Invalid settings: {fields}. Defaults were applied to these fields. "
        "LazyRef will back up the original file before saving changes.",
    "Початкові налаштування збережено: {path}": "Original settings backed up to {path}",
    "Не вдалося зберегти налаштування; поточний пошук доступний.":
        "Could not save settings. You can continue with the current search.",

    # Main window
    "LazyRef — фотореференси для художників": "LazyRef — photo references for artists",
    "Знайди позу. Почни малювати.": "Find a pose. Start drawing.",
    "Галереї": "Galleries",
    "Усі збережені референси й ШІ-зображення, включно з архівами":
        "Browse all saved references and AI images, including archived images",
    "Відкрити папку": "Open folder",
    "Відкрити папку поточної добірки; до першого пошуку — папку збереження":
        "Open the current results folder or the save folder before your first search",
    "Налаштування": "Settings",
    "НОВА ДОБІРКА": "NEW SEARCH",
    "Що шукаємо?": "What should we find?",
    "Опиши позу, ракурс і важливі деталі.": "Describe the pose, camera angle, and important details.",
    "Опис референсу": "Reference description",
    "Чоловік навпочіпки, корпус у ¾, руки витягнуті до камери біля підлоги…":
        "A man squatting in three-quarter view, arms reaching toward the camera near the floor…",
    "Кількість референсів": "Number of references",
    "Менше референсів": "Fewer references",
    "Більше референсів": "More references",
    "Режим пошуку": "Search mode",
    "Джерело фотографій": "Photo source",
    "Обрати зображення з галереї": "Choose an image from the gallery",
    "Обрати фото з комп’ютера": "Choose a photo from your computer",
    "Модель агента Codex, який викликає генератор зображень. Рівень міркування — над галереєю.":
        "The Codex model that calls the image generator. Set the reasoning effort above the results.",
    "Перевірка джерела ON OFF": "Toggle source verification",
    "ON: перевіряти походження й ознаки генерації. OFF: оцінювати позу; джерело необов’язкове, результати неперевірені.":
        "ON checks photo origins and signs of AI generation. OFF reviews pose matches only.",
    "Перевірка джерела: {state}": "Source verification: {state}",
    "Перевірка походження та фотографічності.": "Check the origin and photographic qualities.",
    "Лише відповідність пози. Походження не перевірено.": "Review the pose only. Image origins are unverified.",
    "Пріоритети пошуку": "Search priorities",
    "Точний опис · попередній": "Exact description · legacy",
    "Поза й анатомія · новий": "Pose and anatomy · new",
    "Власні пріоритети": "Custom priorities",
    "Перед пошуком з’явиться список умов: обери обов’язкові, бажані й ті, що можна не враховувати.":
        "Before searching, mark each condition as required, preferred, or ignored.",
    "Головне — поза, ракурс і опора. Одяг, оголеність та оточення — побажання. Для обов’язкових деталей обери «Точний опис».":
        "Prioritize pose, camera angle, and points of support. Clothing, nudity, and setting are preferences. "
        "Choose “Exact description” to require those details.",
    "Попередні правила: поза, ракурс, одяг і сцена з опису враховуються як обов’язкові умови.":
        "Require the pose, camera angle, clothing, and setting specified in the description.",
    "ШІ-варіантів": "AI variants",
    "Кількість ШІ-варіантів": "Number of AI variants",
    "Менше ШІ-варіантів": "Fewer AI variants",
    "Більше ШІ-варіантів": "More AI variants",
    "Відкрити ШІ-зразки": "Open AI guides",
    "Відкрити збережену добірку без повторної генерації": "Open a saved set of AI guides",
    "Зберегти до": "Save to",
    "Папка збереження": "Save folder",
    "Обрати папку збереження": "Choose a save folder",
    "Куди зберігати референси": "Choose where to save references",
    "Обери папку для результатів.": "Choose a folder for the results.",
    "Знайти референси  →": "Find references  →",
    "Створити ШІ-варіанти  →": "Generate AI variants  →",
    "Зупинити пошук": "Stop search",
    "Через твій Codex. Описи й зображення обробляються у хмарі.":
        "Codex processes your descriptions and images in the cloud.",
    "Референси": "References",
    "З налаштувань Codex": "Use Codex default",
    "LazyRef використовує модель із налаштувань Codex.": "LazyRef uses the model configured in Codex.",
    "Рівень міркування": "Reasoning effort",
    "Спільний рівень для нової генерації, пошуку й повторного аналізу. За замовчуванням — Medium.":
        "Applies to generation, search, and resumed reviews. The default is Medium.",
    "Повторити пошук": "Repeat search",
    "Новий пошук обраною моделлю й рівнем міркування за попереднім описом і зразком. Попередня добірка залишається у своїй папці.":
        "Run the search again with the original description and guide, using the selected model and reasoning effort.",
    "Інформація про пошук": "Search information",
    "Розгорнути або згорнути інформацію про пошук": "Expand or collapse search information",
    " · Згорнути": " · Collapse",
    " · Розгорнути": " · Expand",
    "Обери модель й рівень та повтори пошук. Опис і зразок залишаться тими самими; попередні результати не використовуються.":
        "Choose a model and reasoning effort to run a fresh search with the same description and guide.",
    "Це займе більше часу. Хочете перервати пошук?": "The search is taking longer than expected.",
    "Очікування триває. Автоматична зупинка — після 500 секунд поточного виклику.":
        "This call automatically stops after 500 seconds.",
    "Перервати пошук": "Cancel search",
    "Обраний зразок\nУ добірці нижче — знайдені референси.": "Selected guide\nMatching references appear below.",
    "Обрати інший зразок": "Choose another guide",
    "Почни з того, що уявляєш": "Describe the pose you have in mind",
    "Опиши референс — агент шукатиме зображення,\nперевірятиме їх і уточнюватиме пошук до добірки.":
        "Codex will find photos, review them, and refine the search to build your results.",
    "Поза навпочіпки": "Squatting pose",
    "Людина сидить навпочіпки, видно все тіло, ракурс збоку": "A person squatting, full body, side view",
    "Руки в перспективі": "Foreshortened arms",
    "Людина простягає руку до камери, кисть на передньому плані":
        "A person reaching toward the camera, with the hand in the foreground",
    "Готова до пошуку": "Ready to search",
    "Показати перебіг пошуку": "Show search activity",
    "Оцінка ШІ не є підтвердженням походження фото.": "Photo origins are estimated by AI.",
    "Створи ШІ-варіанти, обери позу й шукай референси через Codex.":
        "Generate AI variants, choose a pose, and use Codex to find references.",
    "Обери фото з комп’ютера або галереї та опиши, що потрібно знайти. Власне фото збережеться в галереї як копія.":
        "Choose a photo from your computer or gallery and describe what to look for. Imports are copied to your gallery.",
    "Codex шукає зображення у вебі за описом. Окремі бібліотеки й API-ключі не потрібні.":
        "Codex searches the web for images that match your description.",
    "Широкий пошук.": "Wide search is selected.",
    "Попередній алгоритм.": "Legacy search is selected.",
    "Обери зображення повторно": "Choose the image again",
    "Перевір параметри": "Check search settings",
    "Пошук триває. Складні пози можуть потребувати кількох хвилин.":
        "Search in progress. Complex poses may take a few minutes.",
    "Шукаю референси…": "Finding references…",
    "Модель Codex: {model}": "Codex model: {model}",
    "з налаштувань Codex; у LazyRef не вказана": "from Codex settings",
    "Рівень міркування: {effort}": "Reasoning effort: {effort}",
    "Пріоритети: {profile}": "Priorities: {profile}",
    "OFF — походження не перевірено": "OFF — origin unverified",
    "Алгоритм: {strategy}": "Algorithm: {strategy}",
    "широкий пошук · новий": "wide search · new",
    "попередній пошук · 0.3.4": "legacy search · 0.3.4",
    "Не вдалося повторити пошук": "Could not repeat the search",
    "Повторний пошук: попередній опис і зразок, без використання попередніх результатів.":
        "Started a fresh search using the previous description and guide.",
    "Уточни умови пошуку. Поки вікно відкрите, Codex не працює.":
        "Review the search conditions. Codex is paused while the editor is open.",
    "Зупиняю… Уже завершені результати буде збережено.": "Stopping… Completed results will be saved.",

    "ШІ-варіантів: {variants} · реальних фото після вибору: {photos}":
        "AI variants requested: {variants} · real photos to find after selection: {photos}",
    "Створюю ШІ-варіанти…": "Generating AI variants…",
    "Модель генерації ШІ-зразків: {model} · {effort}": "AI guide generation model: {model} · {effort}",
    "Обери збережені ШІ-зразки": "Choose saved AI guides",
    "ШІ-зразки LazyRef (generation.json)": "LazyRef AI guides (generation.json)",
    "Не вдалося відкрити зразки": "Could not open AI guides",
    "Відкрито збережені ШІ-зразки. Нових викликів генератора немає.": "Opened saved AI guides.",
    "ШІ-варіантів доступно: {count} · запитано: {requested}. Обери зразок для пошуку фото.":
        "AI variants available: {count} · requested: {requested}. Choose a guide to find photos.",
    "ШІ-варіантів створено: {count} із {requested}. Обери зразок для пошуку фото.":
        "Generated {count} of {requested} AI variants. Choose a guide to find photos.",
    "Згенеровані зразки · це не справжні фотографії": "AI-generated pose guides",
    "ШІ-зразки поки не створено": "No AI guides available",
    "Можна повторити генерацію або перейти до пошуку за описом.":
        "Try generating guides again or search using the description.",
    "Генерацію зупинено. Уже створені варіанти збережено; можна обрати будь-який із них.":
        "Generation stopped. You can choose from the variants already saved.",
    "Обери найточнішу позу. ШІ-зразок не буде включено до знайдених фото.":
        "Choose the closest pose to use as a guide for the photo search.",
    "ШІ-ЗРАЗОК ПОЗИ · ВАРІАНТ {index}": "AI POSE GUIDE · VARIANT {index}",
    "Відкрити зразок": "Open guide",
    "Шукати фото з цією позою  →": "Find photos with this pose  →",
    "Обрати ШІ-варіант {index}": "Choose AI variant {index}",
    "Перевір зразок": "Check the guide",
    "ШІ-зразок: {path} · опис: {description}": "AI guide: {path} · description: {description}",

    "Близький збіг": "Close match",
    "Часткова альтернатива": "Partial match",
    "Походження не перевірено": "Origin unverified",
    "Фото: {creator}": "Photo by {creator}",
    "{reason}\n\nВідмінності: {misses}": "{reason}\n\nDifferences: {misses}",
    "Не підтверджено: {criteria}": "Unconfirmed criteria: {criteria}",
    "Відмінності в деталях: {details}": "Differences in details: {details}",
    "Відкрити фото кнопкою нижче. Пропорції збережено.": "Use the button below to open the full photo.",
    "Відкрити фото": "Open photo",
    "Джерело": "Source",
    "{notes}\nВідомості джерела не гарантують автентичність.": "{notes}\nTreat the source information as one clue about authenticity.",
    "Близьких: {close} · часткових: {partial} · запитано: {requested} · оцінено: {examined}":
        "Close matches: {close} · partial matches: {partial} · requested: {requested} · reviewed: {examined}",
    "Збережено {count} із {requested} · оцінено {examined} · повторів {duplicates}":
        "Saved {count} of {requested} · reviewed {examined} · duplicates {duplicates}",
    " · посилань без фото: {count}": " · links only: {count}",
    "Пошук зупинено.": "Search stopped.",
    "Пошук не завершено.": "Search did not finish.",
    "Кандидатів: {discovered} · відбір мініатюр: {screened} · оцінок із кешу: {reused}":
        "Candidates: {discovered} · thumbnails screened: {screened} · cached assessments: {reused}",
    "OFF · Походження не перевірено": "OFF · origin unverified",
    "Посилений відсів повторів: {dedup} · повторів: {duplicates} · Висока роздільність: {quality}":
        "Enhanced duplicate filtering: {dedup} · duplicates: {duplicates} · high resolution: {quality}",
    " · замалих фото відсіяно: {count}": " · low-resolution photos removed: {count}",
    "Модель цієї спроби: {model} · {effort}": "Model used: {model} · {effort}",
    "Відповідних фото поки не знайдено": "No matching photos found yet",
    "Переглянута вибірка не дала достатніх збігів.\nСлабкі результати не додано до добірки; подробиці — у звіті пошуку.":
        "The reviewed photos did not match closely enough. See the search report for details.",
    "Кандидати збережені, аналіз не завершено": "Candidates saved, review incomplete",
    "Фото вже є на комп’ютері. Натисни «Повторити аналіз»,\nщоб оцінити їх без нового пошуку.":
        "Click “Resume review” to assess the photos already saved on your computer.",
    "Оцінки збережені, добірка очікує експорту": "Assessments saved, export pending",
    "Натисни «Повторити збереження», щоб створити добірку\nз уже оцінених фото без нового виклику Codex.":
        "Click “Retry save” to export the photos that have already been reviewed.",
    "Близькі пози — оточення й одяг можуть відрізнятися": "Close poses · setting and clothing may vary",
    "Близькі збіги — усі ключові ознаки": "Close matches · all key criteria met",
    "Часткові альтернативи — частина умов не виконана": "Partial matches",
    "Результати: {count} із {requested}": "Results: {count} of {requested}",
    "Зупинено. Уже оцінені фото й звіт збережено.": "Search stopped. Reviewed photos and the report were saved.",
    "Знайдено близьких збігів: {count}. Часткові альтернативи — окремо в partial.":
        "Close matches found: {count}. Partial matches are saved in the partial folder.",
    "Знайдено близьких збігів: {count}. Посилання й пояснення — у звіті.":
        "Close matches found: {count}. See the report for links and assessments.",
    "Близьких збігів не знайдено. Нижче лише часткові альтернативи; файли — у папці partial.":
        "Partial matches are shown below and saved in the partial folder.",
    "Фото не завантажені. Збережено посилання на роботи для ручного перегляду.":
        "Source links are available for review in your browser.",
    "Збігів недостатньо. Спробуй ширший опис або інше джерело.":
        "Too few matches. Try a broader description or a different source.",
    "Посилання для ручного перегляду": "Links to review in your browser",
    "Переглянути збережені посилання ({count})": "View saved links ({count})",
    "Роботи з обмеженим доступом": "Images with restricted access",
    "Ці зображення не завантажені й не оцінені. Відкрий роботу у своєму браузері; "
    "для 18+ на DeviantArt може знадобитися вхід і Browsing Mode → Mature.":
        "Open these links in your browser to view the images. For 18+ content on DeviantArt, "
        "you may need to sign in and set Browsing Mode to Mature.",
    "Відкрити на DeviantArt": "Open on DeviantArt",

    "Повторити аналіз": "Resume review",
    "Повторити аналіз ({count})": "Resume review ({count})",
    "Повторити збереження": "Retry save",
    "Лише неоцінені фото, по три за раз. Повторна оцінка використовує Codex.":
        "Codex reviews the remaining photos in groups of three.",
    "Зберегти вже оцінені фото та звіт. Новий виклик Codex не потрібен.":
        "Save reviewed photos and their report locally.",
    "Збережені кандидати та добірки": "Saved candidates and results",
    "Продовжити аналіз фото": "Resume photo review",
    "Збережений опис, пріоритети й зразок залишаються тими самими. Спочатку перевіримо фото й відновимо готові оцінки у фоні. "
    "Якщо всі оцінки готові, повторюється лише збереження файлів.":
        "LazyRef checks the photos and recovers completed assessments in the background. "
        "If every photo has been reviewed, it retries the export.",
    "очікує перевірки: {count}": "{count} awaiting review",
    "очікує збереження": "awaiting export",
    "У цій папці немає незавершених оцінок або добірок, що очікують збереження.":
        "No pending reviews or exports in this folder.",
    "Відкрити review.json…": "Open review.json…",
    "Продовжити": "Continue",
    "Обери збережений аналіз": "Choose a saved review",
    "Аналіз LazyRef (review.json)": "LazyRef review (review.json)",
    "Кількість у збереженому запиті має бути від 1 до 40.": "The saved request must specify between 1 and 40 references.",
    "Некоректний режим перевірки джерел у збереженому запиті.": "The saved request has an invalid source verification setting.",
    "Не вдалося відкрити збережений аналіз": "Could not open the saved review",
    "Перевіряю збережені фото…": "Checking saved photos…",
    "Перевіряю збережені фото й відновлюю готові оцінки у фоні.":
        "Checking saved photos and recovering completed assessments in the background.",
    "Аналізую збережені фото…": "Reviewing saved photos…",
    "Зберігаю добірку…": "Saving results…",
    "Продовжую аналіз за збереженим описом, пріоритетами й зразком.":
        "Continuing the review with the saved description, priorities, and guide.",
    "Повторюю збереження вже оцінених фото без виклику Codex.":
        "Retrying the local export of reviewed photos.",

    "Windows не вдалося відкрити папку: {path}": "Windows could not open the folder: {path}",
    "Обери фото для пошуку референсів": "Choose a photo to find references",
    "Зображення (*.png *.jpg *.jpeg *.webp)": "Images (*.png *.jpg *.jpeg *.webp)",
    "Не вдалося імпортувати фото: {error}": "Could not import the photo: {error}",
    "Фото скопійовано в галерею. Оригінал залишився на місці.": "A copy of the photo was added to the gallery.",
    "Не вдалося обрати зразок": "Could not select this guide",
    "Зображення готове до пошуку": "Image ready for search",
    "Уточни, що саме знайти за зразком,\nі натисни «Знайти референси».":
        "Describe what to find in the guide and click “Find references.”",
    "Новий пошук за обраним зображенням або фрагментом.": "Start a new search using the selected image or crop.",
    "Наприклад: черевики в такому ракурсі, як на обраному фрагменті":
        "For example, boots from the same angle as in the selected crop",
    "Обрано для нового пошуку: {filename}\nУточни опис і натисни «Знайти референси».":
        "Selected for a new search: {filename}\nUpdate the description and click “Find references.”",
    "Зразок обрано з галереї. Генерація не потрібна; пошук почнеться після натискання кнопки.":
        "Guide selected from the gallery. Click “Find references” to start the search.",
    "Обрізану копію збережено в галереї: {path}": "Cropped copy saved to the gallery: {path}",
    "Зображення переміщено до Кошика: {filename}": "Moved image to the Recycle Bin: {filename}",

    # Galleries, cropping, and collections
    "LazyRef — галереї": "LazyRef — galleries",
    "Твої зображення": "Your images",
    "Мої добірки…": "My collections…",
    "Оновити": "Refresh",
    "Збережені добірки й ШІ-зразки з поточних папок та архівів.":
        "Browse saved references and AI guides from current and archived folders.",
    "Знайти за описом, моделлю або назвою файла…": "Find by description, model, or filename…",
    "Пошук у галереях": "Search galleries",
    "Усі папки": "All folders",
    "Поточні": "Current",
    "Архів": "Archive",
    "Лише обране": "Favorites only",
    "Показати лише обрані зображення": "Show favorite images only",
    "Зірочки з обох галерей, включно з архівними фото": "Favorites from both galleries, including archived images",
    "Фільтр за добіркою": "Filter by collection",
    "Усі зображення": "All images",
    "Обране й добірки недоступні: {error}": "Favorites and collections are unavailable: {error}",
    "Зірочка — обране. ПКМ → добірки, пошук за фото, обрізання, знайти файл або видалити до Кошика.":
        "Click the star to add an image to Favorites. Right-click for collections, search, crop, locate, or delete.",
    "Переглядаю збережені добірки й архіви…": "Scanning saved results and archives…",
    "Обрізану копію збережено. Можна обрати її для пошуку; оригінал незмінний.":
        "Cropped copy saved. You can select it for a search.",
    "Знайдено {count} зображень.": "Images found: {count}.",
    "Пропущених записів або недоступних папок: {count}.": "Skipped records or inaccessible folders: {count}.",
    "Зображення переміщено до Кошика. Після відновлення натисни «Оновити».":
        "Image moved to the Recycle Bin. Click “Refresh” after restoring it.",
    "Зображень: {count} із {total} · в архіві: {archived}": "Images: {count} of {total} · archived: {archived}",
    "Сторінка {page} із {pages}": "Page {page} of {pages}",
    "Збережених зображень поки немає.": "No saved images yet.",
    "За цим фільтром нічого не знайдено.": "No images match these filters.",
    "← Назад": "← Previous",
    "Далі →": "Next →",
    "ШІ-зображення": "AI images",
    "Референс": "Reference",
    " · фрагмент": " · crop",
    "Мініатюра недоступна": "Thumbnail unavailable",
    "Права кнопка: обрати для пошуку, добірки, обрізати, знайти файл або видалити":
        "Right-click for search, collections, crop, locate, or delete",
    "Добірки: {names}": "Collections: {names}",
    "Відкрити зображення": "Open image",
    "Обрати для пошуку": "Use for search",
    "Шукати референси за цим зображенням": "Find references using this image",
    "Прибрати з обраного": "Remove from Favorites",
    "Додати в обране": "Add to Favorites",
    "Додати до добірок…": "Add to collections…",
    "Знайти зображення": "Locate image",
    "Видалити": "Delete",
    "Перемістити цей файл до Кошика Windows": "Move this file to the Windows Recycle Bin",
    "Це службова копія збереженого аналізу. Видалення недоступне, "
    "щоб не пошкодити відновлення добірки.":
        "This working copy is required to recover the saved review.",
    "Не вдалося виконати дію": "Could not complete the action",
    "Не вдалося відкрити папку зображення.": "Could not open the image folder.",
    "Не вдалося відкрити зображення.": "Could not open the image.",
    "Не вдалося перемістити файл до Кошика. Файл залишено на місці.":
        "Could not move the file to the Recycle Bin. The file is still in its original location.",
    "Зображення завелике для мініатюри": "Image too large to preview",
    "Непідтримуване зображення": "Unsupported image",
    "Файл змінився після відкриття галереї. Натисни «Оновити».": "This file has changed. Click “Refresh” in the gallery.",
    "Файл поза папкою галереї.": "The file is outside the gallery folder.",
    "Посилання на інші файли або папки не підтримуються.": "Choose a file or folder directly.",
    "Зображення недоступне або має непідтримуваний формат.": "The image is unavailable or has an unsupported format.",

    "Обрізати й зберегти копію": "Crop and save a copy",
    "Виділи потрібний фрагмент": "Select an area to crop",
    "Намалюй рамку мишею. Перетягни її або потягни за кути й краї. Оригінал залишиться незмінним.":
        "Drag to draw a selection. Move it by dragging inside, or resize it using the corners and edges.",
    "Що шукатимемо за фрагментом? (можна дописати пізніше)": "What should we look for in this crop? (Optional)",
    "Наприклад: черевики в такому ракурсі": "For example, boots from this angle",
    "Скинути рамку": "Reset selection",
    "Зберегти копію": "Save a copy",
    "Зберегти й обрати для пошуку": "Save and use for search",
    "Фрагмент: {width} × {height} px": "Crop: {width} × {height} px",
    "Виділи область не менше {side} × {side} px.": "Select an area of at least {side} × {side} px.",
    "Для обрізання потрібен файл до 50 МБ.": "Choose a file of 50 MB or less to crop.",
    "Непридатний формат або розмір зображення.": "The image format or size is unsupported.",
    "Зображення завелике для обрізання.": "This image is too large to crop.",
    "Некоректна рамка обрізання.": "Select a valid crop area.",
    "Рамка має бути всередині зображення.": "Keep the selection within the image.",
    "Виділи фрагмент не менше 32 × 32 пікселів.": "Select an area of at least 32 × 32 pixels.",
    "Невідомий тип зображення.": "Unknown image type.",
    "Підпис фрагмента має містити до 2000 символів.": "Enter up to 2,000 characters for the crop description.",
    "Копію можна зберегти лише в локальну папку програми.": "Save the copy in the local LazyRef folder.",
    "Папка копій не може бути посиланням на інший каталог.": "Choose a regular folder to store copies.",
    "Копія завелика для повторного аналізу (до 20 МБ). Виділи меншу область.":
        "The copy exceeds the 20 MB review limit. Select a smaller area.",

    "Добірки для зображення": "Image collections",
    "Мої добірки": "My collections",
    "Додати до добірок": "Add to collections",
    "Познач усі потрібні добірки. Файл залишиться один.":
        "Select all the collections where you want this image to appear.",
    "Створюй добірки для проєктів і деталей: руки, черевики, персонажі…":
        "Create collections for projects or subjects such as hands, boots, or characters.",
    "Добірки зображень": "Image collections",
    "Назва нової добірки": "New collection name",
    "Назва добірки": "Collection name",
    "Створити": "Create",
    "Перейменувати": "Rename",
    "Видалити добірку": "Delete collection",
    "Видаляє лише добірку. Фото залишаються на місці.": "Delete the collection and keep its photos.",
    "Зняття позначки або видалення добірки не видаляє фотографій.":
        "Photos remain in the galleries when removed from a collection.",
    "Створи першу добірку вище.": "Enter a name above to create your first collection.",
    "Введи назву добірки.": "Enter a collection name.",
    "Назва добірки має містити від 1 до 80 видимих символів.": "Use 1 to 80 visible characters for the collection name.",
    "Обране та добірки зберігаються лише локально.": "Favorites and collections are stored locally.",
    "Папка обраного не може бути посиланням на інший каталог.": "Use a regular folder to store favorites.",
    "Непридатний службовий файл добірок.": "Invalid collections data file.",
    "Файл добірок недоступний або завеликий.": "The collections file is unavailable or too large.",
    "Невідомий формат файлу добірок; його залишено без змін.": "Unknown collections file format.",
    "Непідтримуваний формат файлу добірок; його залишено без змін.": "Unsupported collections file format.",
    "Не вдалося прочитати або зберегти обране й добірки. Попередні дані не перезаписано.":
        "Could not read or save favorites and collections. The previous data was preserved.",
    "Некоректний запис добірки.": "Invalid collection entry.",
    "Неповний запис добірки.": "Incomplete collection entry.",
    "Добірка з такою назвою вже існує.": "A collection with this name already exists.",
    "Добірку вже видалено. Онови список.": "This collection was deleted. Refresh the list.",
    "Некоректна позначка обраного.": "Invalid favorite status.",
    "Некоректний список добірок.": "Invalid collection list.",

    "Власні пріоритети пошуку": "Custom search priorities",
    "Що важливо знайти?": "What matters for this search?",
    "Обери обов’язкові умови, бажані деталі й те, що можна не враховувати. Формулювання можна змінити.":
        "Edit each criterion and choose whether it is required, preferred, or ignored.",
    "Зразок недоступний для попереднього перегляду": "Guide preview unavailable",
    "+ Додати умову": "+ Add criterion",
    "Обов’язково — визначає близький збіг. Бажано — допомагає обрати кращі фото. Не враховувати — не впливає на пошук та оцінку.":
        "Required criteria define a close match. Preferred criteria help rank photos. Ignored criteria are skipped.",
    "Скасувати пошук": "Cancel search",
    "Почати пошук із цими умовами": "Start search with these criteria",
    "Наприклад: черевик спирається на носок": "For example, the boot rests on its toe",
    "Умова пошуку": "Search criterion",
    "Важливість умови": "Criterion priority",
    "Прибрати умову": "Remove criterion",
    "Прибрати цю умову зі списку": "Remove this criterion from the list",
    "Обов’язково": "Required",
    "Бажано": "Preferred",
    "Не враховувати": "Ignore",
    "Додай від 1 до 10 умов пошуку.": "Add 1 to 10 search criteria.",
    "Перевір умови: непорожній опис до 240 символів і вибраний пріоритет.":
        "Enter 1 to 240 characters and choose a priority for each criterion.",
    "Познач хоча б одну умову як «Обов’язково».": "Mark at least one criterion as required.",
    "Власні пріоритети потребують підтвердження умов перед пошуком.": "Confirm your custom criteria before starting the search.",

    # Usage report
    "Витрати Codex": "Codex usage",
    "Облік токенів": "Token usage",
    "Відкрити файл звіту": "Open report file",
    "немає даних": "unavailable",
    " · неповні": " · incomplete",
    "Генерація ШІ-зразків": "AI guide generation",
    "Планування": "Planning",
    "Пошук зображень": "Image search",
    "Попередній відбір мініатюр": "Thumbnail screening",
    "Перевірка джерел": "Source verification",
    "Аналіз фото": "Photo review",
    "Інші виклики": "Other calls",
    "Це токени, повідомлені Codex CLI. Витрати інструмента генерації зображень та інших "
    "сервісів можуть не мати окремого лічильника в цьому звіті. Це не відсоток ліміту й не сума грошей.":
        "These are the token counts reported by Codex CLI. Image generation and other tools may report usage separately.",
    "Не вдалося записати облік витрат на диск. Показано дані з пам’яті.": "Could not save usage to disk. Showing usage for this session.",
    "Для цього запуску статистику токенів не збережено.": "No token usage was saved for this run.",
    "{title}: викликів не було.": "{title}: zero calls.",
    " (неповні дані)": " (incomplete data)",
    "{title}: {total} токенів за отриманими лічильниками": "{title}: {total} tokens reported",
    "  Вхід: {input} · вихід: {output}": "  Input: {input} · output: {output}",
    "  Із входу — кеш: {cached}; із виходу — міркування: {reasoning}":
        "  Cached input: {cached} · reasoning output: {reasoning}",
    "  Запусків Codex: {count} · з повними лічильниками: {complete}":
        "  Codex calls: {count} · complete token counts: {complete}",
    "  Облік неповний: невідома витрата незавершених/неповністю звітованих запусків не дорівнює нулю.":
        "  The total may be higher because some calls have missing usage data.",
    "Поточний запуск": "Current run",
    "Попередні спроби цієї добірки (не входять у поточний запуск):": "Previous attempts for these results",
    "Спроба {index}": "Attempt {index}",
    "Разом усі спроби цієї добірки": "Total for all attempts",
    "Генерація обраного пакета зразків — окремо від поточного пошуку:": "Usage for generating the selected AI guides",
    "Раніше створені ШІ-варіанти": "Previously generated AI variants",
    "Це весь пакет варіантів. При повторному пошуку за ним генерація не нараховується заново.":
        "This covers the full batch. Reusing a guide uses search tokens only.",
    "Даних немає: для цього пакета облік не збережено.": "No saved usage data for this batch.",
    "Разом = вхід + вихід. Кеш і міркування повторно не додаються.":
        "Total = input + output. Input includes cached tokens, and output includes reasoning tokens.",

    # Search, generation, and review progress
    "Codex уточнює опис і готує пошукові запити…": "Codex is refining the description and preparing search queries…",
    "Шукаю: {summary}": "Searching for: {summary}",
    "Відхилено позначену генерацію: {title}": "Skipped an image marked as generated: {title}",
    "Фото {width} × {height} px замале: потрібно від 1200 × 800 px (або вертикально).":
        "Photo at {width} × {height} px is too small. Minimum dimensions are 1200 × 800 px in either orientation.",
    "Codex переглядає {count} фото · уже оцінено {examined}": "Codex is reviewing {count} photos · reviewed so far: {examined}",
    "Оцінено {examined} · близьких {close} · часткових {partial} · повторів {duplicates}":
        "Reviewed {examined} · close matches {close} · partial matches {partial} · duplicates {duplicates}",
    "Завантажую кандидат {attempt} · у вибірці {count}…": "Downloading candidate {attempt} · candidates found: {count}…",
    "Пряме завантаження не вдалося. Пробую скріншот фото…": "Download failed. Trying a screenshot…",
    "Цикл агента {stage}/{total} · шукаю нові фото з урахуванням попередніх оцінок…":
        "Agent round {stage}/{total} · finding new photos using earlier assessments…",
    "Близьких збігів поки недостатньо.": "Too few close matches so far.",
    "Переглядаю наступні сторінки…": "Checking more pages…",
    "Шукаю інші формулювання тієї ж сцени…": "Trying other ways to describe the scene…",
    "Оцінюю знайдених кандидатів…": "Reviewing candidates…",
    "Етап {stage}/{total} · нових кандидатів {added}, загалом {count}. Перевіряю фото…":
        "Stage {stage}/{total} · {added} new candidates, {count} total. Checking photos…",
    "Зупиняю пошук і зберігаю вже оцінені фото…": "Stopping the search and saving reviewed photos…",
    "Зберігаю добірку та посилання на джерела…": "Saving photos and source links…",
    "Codex пропонує умови. Потім ти зможеш уточнити їх перед пошуком…":
        "Codex is drafting criteria for you to review before the search…",
    "Codex складає план і описує позу для пошуку…": "Codex is planning the search and describing the pose…",
    "Використовую збережений план того самого запиту й зразка.": "Reusing the saved plan for this request and guide.",
    "Швидкий відбір: {count} мініатюр · уже переглянуто {screened}":
        "Screening {count} thumbnails · screened so far: {screened}",
    "Широкий пошук {round}/{total} · кандидатів {count}/{limit}": "Wide search round {round}/{total} · candidates {count}/{limit}",
    "Завантажую відібране фото: {title}": "Downloading selected photo: {title}",
    "Перевіряю джерела лише відібраних фото: {count}": "Checking sources for {count} selected photos",
    "Детальний аналіз {count} фото · нових оцінок {examined}/{limit}":
        "Reviewing {count} photos in detail · new assessments {examined}/{limit}",
    "Зберігаю фото та звіт усіх етапів пошуку…": "Saving photos and the search report…",
    "сторінка": "page",
    "Codex перевіряє джерело: {host}": "Codex is checking the source: {host}",
    "Codex шукає зображення: {query}": "Codex is searching for images: {query}",
    "Codex створює ШІ-варіант {variant}/{count}. Це може тривати кілька хвилин…":
        "Codex is generating AI variant {variant}/{count}. This may take a few minutes…",
    "Codex повернув {count} файли за один виклик; збережено нових зразків: {saved}.":
        "Codex returned {count} files in one call. New guides saved: {saved}.",
    "Файл ШІ-варіанта знайдено в папці цього запуску Codex.": "Found the generated image in the folder for this Codex run.",
    "Повторний аналіз: {batch} фото · завершено {reviewed} із {pending}. Новий пошук не запускається.":
        "Reviewing {batch} saved photos · completed {reviewed} of {pending}.",
    "Зберігаю добірку з попередніми та новими завершеними оцінками…":
        "Saving results with the completed assessments…",

    "Опиши референс: від 5 до 2000 символів.": "Describe the reference in 5 to 2,000 characters.",
    "Невідомий режим зразка.": "Unknown guide mode.",
    "Обери зображення для пошуку з галереї або комп’ютера.": "Choose an image from the gallery or your computer to search with.",
    "Кількість має бути від 1 до 40.": "Choose between 1 and 40 references.",
    "Ліміт кандидатів має бути від 8 до 120.": "Choose a candidate limit between 8 and 120.",
    "Кількість циклів агента має бути від 1 до 8.": "Choose between 1 and 8 agent rounds.",
    "Обери відомий варіант пріоритетів пошуку.": "Choose an available search priority profile.",
    "Ручні умови потребують режиму «Власні пріоритети».": "Select Custom priorities to use your own criteria.",
    "Невідомий алгоритм пошуку.": "Unknown search algorithm.",
    "Перевірка джерела має бути ON або OFF.": "Set source verification to ON or OFF.",
    "Фільтри повторів і якості мають бути ON або OFF.": "Set the duplicate and resolution filters to ON or OFF.",
    "Новий пошук: 20–300 кандидатів і 8–120 детальних оцінок.":
        "Wide search supports 20–300 candidates and 8–120 detailed assessments.",
    "Пошук за зображенням працює в режимі агента Codex.": "Use Codex agent mode to search by image.",
    "Пошук працює лише через агент Codex.": "Select the Codex agent to search.",
    "Обери рівень міркування: Low, Medium, High або Extra high.": "Choose a reasoning effort of Low, Medium, High, or Extra high.",
    "Опис має містити до 2000 символів.": "Enter a description of up to 2,000 characters.",
    "Обери фото розміром до 20 МБ.": "Choose a photo of 20 MB or less.",
    "Потрібне PNG, JPG або WEBP: від 32 пікселів на сторону, до 40 мегапікселів.":
        "Choose a PNG, JPG, or WEBP at least 32 pixels on each side and up to 40 megapixels.",
    "Обери звичайне фото або окремий кадр замість анімації.": "Choose a still photo or a single animation frame.",
    "Не вдалося прочитати фото. Обери інший файл.": "Could not read this photo. Choose another file.",
    "Копію можна зберегти лише в локальній папці програми.": "Save the copy in the local LazyRef folder.",
    "Зображення-зразок відсутнє або завелике (до 50 МБ).": "The guide is missing or exceeds 50 MB.",
    "Непридатний формат або розмір зображення-зразка.": "The guide image format or size is unsupported.",
    "Не вдалося прочитати зразок. Обери інше зображення.": "Could not read the guide. Choose another image.",
    "Кількість ШІ-варіантів має бути від 1 до 8.": "Choose between 1 and 8 AI variants.",
    "Не вдалося відкрити звіт із ШІ-зразками.": "Could not open the AI guide report.",
    "Це не добірка ШІ-зразків LazyRef.": "Choose a set of AI guides created with LazyRef.",
    "У добірці немає збережених зразків або пошкоджені дані.": "The guide set is empty or its data is damaged.",
    "Некоректний шлях до збереженого ШІ-зразка.": "Invalid path to a saved AI guide.",
    "Зразок має бути в папці обраної добірки.": "The guide must be in the selected folder.",
    "Збережений ШІ-зразок змінений або його контрольна сума пошкоджена.":
        "A saved guide has changed or its checksum is damaged.",
    "Усі зразки цієї добірки видалені або переміщені.": "All guides in this set have been deleted or moved.",
    "Стара добірка не містить контрольних сум усіх зразків; цілісність цих файлів не підтверджено.":
        "Some guides in this older set are missing checksums.",
    "Codex не підтвердив ідентифікатор цього запуску генерації.": "Codex did not confirm the generation run ID.",
    "Codex не повернув один файл ШІ-варіанта.": "Codex did not return a file for one of the AI variants.",
    "Codex не повернув файл із папки своєї генерації.": "Codex did not return a file from its generation folder.",
    "Час створення ШІ-зразка не відповідає цьому запуску генерації.": "The image timestamp falls outside this generation run.",
    "Codex повернув непридатні файли ШІ-зразка. Уже створені варіанти збережено.":
        "Codex returned unusable image files. Guides from earlier in the run were saved.",
    "Codex повернув повторний варіант. Уже створені зразки збережено.":
        "Codex returned a duplicate variant. Guides from earlier in the run were saved.",
    "Обраний файл Codex не знайдено. Вкажи codex.exe в налаштуваннях.":
        "The selected Codex executable is missing. Choose codex.exe in Settings.",
    "Codex CLI не знайдено. Вкажи шлях до codex.exe в налаштуваннях.":
        "Codex CLI was not found. Enter the path to codex.exe in Settings.",
    "Потрібно увійти в Codex CLI: відкрий термінал і виконай codex login.":
        "Open a terminal and run codex login to sign in to Codex CLI.",
    "Вичерпано ліміт Codex. Спробуй пізніше або перевір ліміти свого плану.":
        "You have reached your Codex usage limit. Try again later or check your plan limits.",
    "Codex не бачить профіль Windows. Запусти LazyRef звичайним подвійним кліком.":
        "Codex could not find your Windows profile. Double-click the LazyRef shortcut to launch it.",
    "Codex не повернув оцінку. Спробуй ще раз.": "Codex did not return an assessment. Try again.",
    "Не вдалося прочитати відповідь Codex.": "Could not read the Codex response.",
    "Codex повернув неочікуваний формат відповіді.": "Codex returned an unexpected response format.",
    "Невідомі пріоритети пошуку.": "Unknown search priorities.",
    "Codex не сформував пошукові запити.": "Codex did not produce search queries.",
    "Codex не сформував критерії пошуку.": "Codex did not produce search criteria.",
    "Codex не визначив персонажа й механіку пози. Неповний план не використовується.":
        "The search plan is missing the subject and pose mechanics.",
    "Codex не описав позу обраного зразка. Пошук за неповним планом не починається.":
        "The search plan is missing the pose description for the selected guide.",
    "Codex не сформував план розширення пошуку.": "Codex did not produce a plan to broaden the search.",
    "Власні умови не підтверджено.": "Confirm your custom criteria first.",
    "Codex оцінив не всі фотографії. Пошук зупинено, щоб не зберегти неперевірені файли.":
        "Search stopped because Codex returned an incomplete set of photo assessments.",
    "Не вдалося зіставити оцінки Codex із фотографіями.": "Could not match the Codex assessments to the photos.",
    "Codex повернув невідому категорію зображення.": "Codex returned an unknown image category.",
    "Codex повернув некоректну оцінку.": "Codex returned an invalid assessment.",
    "Codex не визначив придатність зображення як художнього референсу.":
        "The Codex assessment is missing a verdict on the image’s usefulness as a reference.",
    "Codex повернув некоректне пояснення.": "Codex returned an invalid explanation.",
    "Codex не перевірив усі обов’язкові ознаки. Неповна оцінка не зберігається.":
        "The assessment is missing required criteria and could not be saved.",
    "Аналіз цієї добірки вже триває в іншому вікні.": "This review is already running in another window.",
    "Локальне збереження неможливе: є неоцінені фото або готові оцінки не відповідають збереженим умовам. Новий аналіз не запускався.":
        "Local export requires a completed assessment for each photo under the saved criteria.",
    "Усі збережені кандидати вже оцінені. Повторний виклик Codex не потрібен.": "All saved candidates have already been reviewed.",
    "У перевіреній вибірці немає достатньо відповідних фото. Слабкі збіги не зберігались.":
        "Too few of the reviewed photos matched the requested pose.",
    "У перевіреній вибірці немає достатньо відповідних фото.": "Too few of the reviewed photos matched.",
    "Близьких збігів не знайдено. Збережено лише часткові альтернативи з указаними відсутніми ознаками.":
        "Partial matches were saved with details of which criteria they missed.",
    "Збережено часткові альтернативи; близьких збігів не знайдено.": "Partial matches were saved.",
}
