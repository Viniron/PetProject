/**
 * Файл захвата до отправки: что принять, как ужать, сколько в нём страниц.
 *
 * Сервер проверяет всё сам и по байтам (ADR-054): формат, размер, число
 * страниц PDF. Здесь - только то, что экономит owner время и трафик, и ни
 * одно из этих правил не решает судьбу файла вместо сервера.
 */

/** Что предлагается в выборе файла. Решает сервер; это подсказка диалогу. */
export const ПРИНИМАЕМЫЕ = "image/jpeg,image/png,image/webp,application/pdf";

// Длинная сторона снимка после ужатия. Больше модель всё равно уменьшит
// у себя, а по сотовой сети лишние мегабайты - это секунды перед «Извлекаю».
const ДЛИННАЯ_СТОРОНА = 1600;
// Ниже этого размера снимок уходит как есть: пережатие маленького файла
// только теряет качество.
const НЕ_ЖАТЬ_ДО_БАЙТ = 1_000_000;
const КАЧЕСТВО_JPEG = 0.85;

export function этоPdf(файл: File): boolean {
  return файл.type === "application/pdf" || файл.name.toLowerCase().endsWith(".pdf");
}

/**
 * Снимок с телефона весит несколько мегабайт - ужимаем в браузере.
 *
 * Не вышло (браузер без canvas, формат не читается) - уходит оригинал:
 * сервер его примет или откажет с причиной, а ужатие - удобство, не условие.
 */
export async function ужать(файл: File): Promise<File> {
  if (этоPdf(файл) || файл.size <= НЕ_ЖАТЬ_ДО_БАЙТ) return файл;
  try {
    const картинка = await createImageBitmap(файл);
    const доля = Math.min(1, ДЛИННАЯ_СТОРОНА / Math.max(картинка.width, картинка.height));
    const холст = document.createElement("canvas");
    холст.width = Math.round(картинка.width * доля);
    холст.height = Math.round(картинка.height * доля);
    холст.getContext("2d")?.drawImage(картинка, 0, 0, холст.width, холст.height);
    картинка.close();
    const снимок = await new Promise<Blob | null>((готово) =>
      холст.toBlob(готово, "image/jpeg", КАЧЕСТВО_JPEG),
    );
    if (!снимок || снимок.size >= файл.size) return файл;
    const имя = файл.name.replace(/\.[^.]+$/, "") + ".jpg";
    return new File([снимок], имя, { type: "image/jpeg" });
  } catch {
    return файл;
  }
}

/**
 * Число страниц PDF, если его видно без библиотеки. `null` - не видно.
 *
 * Считаются объекты `/Type /Page`. В PDF со сжатыми потоками объектов их
 * не видно вовсе - тогда превью показывает только размер, а точное число
 * назовёт сервер, если файл длиннее предела. Угадывать здесь нечего.
 */
export async function страницPdf(файл: File): Promise<number | null> {
  try {
    const байты = new Uint8Array(await файл.arrayBuffer());
    const текст = new TextDecoder("latin1").decode(байты);
    const найдено = текст.match(/\/Type\s*\/Page(?![a-zA-Z])/g)?.length ?? 0;
    return найдено > 0 ? найдено : null;
  } catch {
    return null;
  }
}

/** «380 КБ», «1,4 МБ» - как в макете. */
export function размер(байт: number): string {
  if (байт < 1_000_000) return `${Math.max(1, Math.round(байт / 1000))} КБ`;
  return `${(байт / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: 1 })} МБ`;
}

/** «1 страница», «2 страницы», «5 страниц». */
export function страниц(число: number): string {
  const последняя = число % 10;
  const две = число % 100;
  const слово =
    последняя === 1 && две !== 11
      ? "страница"
      : последняя >= 2 && последняя <= 4 && (две < 12 || две > 14)
        ? "страницы"
        : "страниц";
  return `${число} ${слово}`;
}
