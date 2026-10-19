async def choose_option(page, selector, value):
    """Use the app's rendered menu, retaining native form assertions in tests."""
    select = page.locator(selector)
    trigger = select.locator('..').locator('.custom-select-trigger')
    await trigger.click()
    menu_id = await trigger.get_attribute('aria-controls')
    option = page.locator(f'#{menu_id} [role="option"]')
    for index in range(await option.count()):
        row = option.nth(index)
        if await row.get_attribute('data-value') == value:
            await row.click()
            return
    raise AssertionError(f'Missing menu option {value} in {selector}')
