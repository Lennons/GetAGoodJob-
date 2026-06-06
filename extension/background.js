chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({ bcaInstalledAt: new Date().toISOString() });
});
