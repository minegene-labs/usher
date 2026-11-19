// Usher — the app. Plain web components and ES modules, no framework, no build step.
// Everything the page shows comes from Usher's own server; nothing loads from anywhere else.
// Each screen lives in its own module as a mixin that adds its methods to the app element.
import { Shell } from "./shell.js";
import { Home } from "./home.js";
import { Downloads } from "./downloads.js";
import { UpcomingLibrary } from "./upcoming.js";
import { Status } from "./status.js";
import { Settings } from "./settings.js";
import { Notify } from "./notify.js";
import { Advanced } from "./advanced.js";
import { Updates } from "./updates.js";
import { Sheets } from "./sheets.js";
import { Search } from "./search.js";
import { Discover } from "./discover.js";
import { Account } from "./account.js";
import { Setup } from "./setup.js";
import { Changelog } from "./changelog.js";

const screens = [Shell, Home, Downloads, UpcomingLibrary, Status, Settings, Notify, Advanced, Updates, Sheets, Search, Discover, Account, Setup, Changelog];
const UsherApp = screens.reduce((Base, addScreen) => addScreen(Base), HTMLElement);

customElements.define("usher-app", UsherApp);
