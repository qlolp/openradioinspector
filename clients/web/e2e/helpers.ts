import { expect, type Page } from '@playwright/test';
export async function acceptLegacyCore(page: Page) {
  await page.getByTestId('legacy-compatibility-consent').check();
  await page.getByTestId('legacy-compatibility-continue').click();
  await expect(page.getByTestId('workspace-heading')).toBeVisible();
}
export async function openDesk(page: Page) {
  await page.goto('./');
  await acceptLegacyCore(page);
}
