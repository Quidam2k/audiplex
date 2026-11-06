package com.audiplex.app.ui.settings

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

// #5422: the Voices row in Settings > Levels.
class PantheonVoicesTest {
    @Test
    fun installedRowSaysWhereTheSliderIs() {
        assertEquals("In the Pantheon app: tap the gear.", PantheonVoices.rowText(installed = true))
    }

    @Test
    fun missingAppSaysSoInsteadOfOfferingAButton() {
        val text = PantheonVoices.rowText(installed = false)
        assertNotEquals(PantheonVoices.rowText(installed = true), text)
        assert("isn't on this phone" in text)
    }

    @Test
    fun linksToThePantheonPackage() {
        assertEquals("com.pantheon.app", PantheonVoices.PACKAGE)
    }
}
