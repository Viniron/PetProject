import { ThemePicker } from "@/settings/ThemePicker";

/**
 * Настройки - вкладка вне генерируемого списка курсов (этап 1 дизайна),
 * экран - макет `design/mockups/16-settings-theme.html`.
 *
 * Настраивается здесь одна тема, и только потому, что она свойство
 * устройства (ADR-057). Всё, что относится к плате, живёт в `.env`
 * и таблице `settings`, и трогать это через интерфейс никто не просил.
 */
export default function SettingsPage() {
  return (
    <div className="content-col">
      <h1 className="screen-title">Settings</h1>
      <p className="screen-sub">Оформление и то, что настраивается на плате</p>

      <h2 className="set-head">Тема</h2>
      <ThemePicker />
      {/* Без подписи светлый телефон при тёмном ПК читался бы как поломка. */}
      <p className="set-hint">Выбор действует на этом устройстве.</p>

      <h2 className="set-head">Остальное</h2>
      <p className="set-text">
        Часовой пояс, окно забора расписания и ключи живут на плате — в <code>.env</code> и таблице{" "}
        <code>settings</code>. Меняются там, а не здесь.
      </p>
    </div>
  );
}
