/*
 * View model for TheSpaghettiDetective Wizard
 *
 * Author: The Spaghetti Detective
 * License: AGPLv3
 */
$(function () {

    function apiCommand(data) {
        return $.ajax("api/plugin/thespaghettidetective", {
            method: "POST",
            contentType: "application/json",
            data: JSON.stringify(data)
        });
    }

    function ThespaghettidetectiveWizardViewModel(parameters) {
        var self = this;

        // assign the injected parameters, e.g.:
        // self.loginStateViewModel = parameters[0];
        self.settingsViewModel = parameters[0];

        self.step = ko.observable(1);
        self.mobileFlow = ko.observable(true);
        self.securityCode = ko.observable('');
        self.verifying = ko.observable(false);
        self.userAgreementChecked = ko.observable(true);
        self.printerName = ko.observable('');
        self.printerNameTimeoutId = ko.observable(null);
        self.ctrlDown = ko.observable(false); // Handling Ctrl+V / Cmd+V commands
        self.currentFeatureSlide = ko.observable(1);

        let ctrlKey = 17, cmdKey = 91, vKey = 86;

        self.nextStep = function() {
            self.step(self.step() + 1);
        };

        self.toStep = function(step) {
            self.step(step);
        };

        self.prevStep = function() {
            self.step(self.step() - 1);
        };

        self.toggleCheck = function() {
            self.userAgreementChecked(!self.userAgreementChecked());
        }

        self.securityCode.subscribe(function(code) {
            self.verifySecurityCode(code);
        });

        self.printerName.subscribe(function() {
            if (self.printerNameTimeoutId()) {
                clearTimeout(self.printerNameTimeoutId());
            }
            let newTimeoutId = setTimeout(self.savePrinterName, 1000);
            self.printerNameTimeoutId(newTimeoutId);
        })

        self.savePrinterName = function() {
            var newName = self.printerName().trim();
            if (newName.length == 0) {
                return;
            }

            // Saving in progress animation
            $('.printerNameInput').addClass('saving-in-progress');
            $('.printerNameInput .error-message').hide();

            apiCommand({
                command: "update_printer",
                name: newName})
                .done(function(result) {
                    $('.printerNameInput').removeClass('saving-in-progress');

                    if (result.succeeded) {
                        $('.printerNameInput').addClass('successfully-saved');
                        setTimeout(() => $('.printerNameInput').removeClass('successfully-saved'), 2000);
                    } else {
                        $('.printerNameInput').addClass('error-occurred');
                        setTimeout(() => $('.printerNameInput').removeClass('error-occurred'), 2000);
                        $('.printerNameInput .error-message').show();
                    }
                })
                .fail(function() {
                    $('.printerNameInput').removeClass('saving-in-progress');
                    $('.printerNameInput').addClass('error-occurred');
                    setTimeout(() => $('.printerNameInput').removeClass('error-occurred'), 2000);
                    $('.printerNameInput .error-message').show();
                });
        }

        $(document).keydown(function(e) {
            if (self.step() === 4) {
                // Check if user isn't trying to input into specific input
                let focusedElem = document.activeElement;
                if (focusedElem instanceof HTMLInputElement && focusedElem.type === 'text') {
                    return true;
                }

                let availableInputs = ['0','1','2','3','4','5','6','7','8','9'];

                if (e.keyCode === 8) {
                    // Backspace
                    for (let i = 6; i >= 1; i--) {
                        let input = $('.verification-code-input input[data-number='+ i +']');
                        if (input.val() && input.val() !== '|') {
                            input.val('|').addClass('active');

                            if (i < 6) {
                                $('.verification-code-input input[data-number='+ (i + 1) +']').val("").removeClass('active');
                            }

                            self.securityCode(self.securityCode().slice(0, -1));
                            break;
                        }
                    }
                } else if (availableInputs.includes(e.key)) {
                    for (let i = 1; i <= 6; i++) {
                        let input = $('.verification-code-input input[data-number='+ i +']');
                        if (!input.val() || input.val() === '|') {
                            input.val(e.key);
                            input.removeClass('active');

                            if (i < 6) {
                                $('.verification-code-input input[data-number='+ (i + 1) +']').val("|").addClass('active');
                            }

                            self.securityCode(self.securityCode() + e.key);
                            break;
                        }
                    }
                }

                if (self.securityCode().length < 6) {
                    // Return input to initial state
                    $('.verification-wrapper').removeClass(['error', 'success', 'unknown']);
                }
            }
        });

        // Next feature in the slider on home screen
        self.nextFeature = function() {
            let container = $('.features').last();
            let slidesCount = container.find('.feature').length;
            let currentSlide = self.currentFeatureSlide();
            let nextSlide = currentSlide === slidesCount ? 1 : currentSlide + 1;

            container.find('.feature[data-number="'+ currentSlide +'"]').animate({
                left: '-100%'
            }, {duration: 500, queue: false});

            container.find('.feature[data-number="'+ nextSlide +'"]').animate({
                left: '0'
            },
            500,
            function() {
                let next = nextSlide === slidesCount ? 1 : nextSlide + 1;
                container.find('.feature[data-number="'+ next +'"]').css('left', '100%');
            });

            self.currentFeatureSlide(nextSlide);
        }

        setInterval(self.nextFeature, 3000);


        // Functionality to handle Ctrl+V or Cmd+V commands

        document.addEventListener('keydown', function(e) {
            if (e.keyCode == ctrlKey || e.keyCode == cmdKey) self.ctrlDown = true;
        });
        document.addEventListener('keyup', function(e) {
            if (e.keyCode == ctrlKey || e.keyCode == cmdKey) self.ctrlDown = false;
        });

        document.addEventListener('keydown', function(e) {
            if (self.ctrlDown && (e.keyCode == vKey)) {
                // Check if user isn't trying to input into specific input
                let focusedElem = document.activeElement;
                if (focusedElem instanceof HTMLInputElement && focusedElem.type === 'text') {
                    return true;
                }

                self.pasteFromClipboard();
            } else {
                return true;
            }
        });

        self.pasteFromClipboard = function() {
            let clipboardPlaceholder = $('textarea.paste-from-clipboard-placeholder').last();
            clipboardPlaceholder.val('').focus();

            setTimeout(function() {
                let text = clipboardPlaceholder.val();
                clipboardPlaceholder.val('').blur();
                let format = new RegExp("\\d{6}");

                if (format.test(text)) {
                    $('.verification-wrapper').removeClass(['error', 'success', 'unknown']);
                    self.securityCode('');
                    for (let i = 1; i <= 6; i++) {
                        let input = $('.verification-code-input input[data-number='+ i +']');
                        input.val(text[i - 1]).removeClass('active');
                        self.securityCode(self.securityCode() + text[i - 1]);
                    }
                }
            }, 100)

            return true;
        };


        self.verifySecurityCode = function(code) {
            if (code.length !== 6) {
                return;
            }
            self.verifying(true);

            apiCommand({
                command: "verify_code",
                code: code,
                endpoint_prefix: $('#endpoint_prefix-input').val(),
            })
                .done(function(apiStatus) {
                    if (apiStatus.succeeded == null) {
                        $('.verification-wrapper').addClass('unknown');
                    }
                    else if (apiStatus.succeeded) {
                        $('.verification-wrapper').addClass('success');
                        self.printerName(apiStatus.printer.name);
                        self.nextStep();
                    } else {
                        $('.verification-wrapper').addClass('error');
                    }
                })
                .fail(function() {
                    $('.verification-wrapper').addClass('unknown');
                })
                .always(function () {
                    self.verifying(false);
                });
        };

        self.resetEndpointPrefix = function () {
            self.settingsViewModel.settings.plugins.thespaghettidetective.endpoint_prefix("https://app.thespaghettidetective.com");
        };

        self.reset = function() {
            self.step(1);
            self.verifying(false);
            self.securityCode('');

            let verificationWrapper = $('.verification-wrapper');
            verificationWrapper.removeClass('success error unknown');

            for (let i = 1; i <= 6; i++) {
                let input = verificationWrapper.find('.verification-code-input input[data-number='+ i +']');

                // Clear cells and insert visual cursor in first cell
                if (i === 1) {
                    input.val('|').addClass('active');
                } else {
                    input.val('')
                }
            }
        }
    }

    /* view model class, parameters for constructor, container to bind to
     * Please see http://docs.octoprint.org/en/master/plugins/viewmodels.html#registering-custom-viewmodels for more details
     * and a full list of the available options.
     */
    OCTOPRINT_VIEWMODELS.push({
        construct: ThespaghettidetectiveWizardViewModel,
        // ViewModels your plugin depends on, e.g. loginStateViewModel, settingsViewModel, ...
        dependencies: ["settingsViewModel"],
        // Elements to bind to, e.g. #settings_plugin_thespaghettidetective, #tab_plugin_thespaghettidetective, ...
        elements: [
            "#wizard_plugin_thespaghettidetective",
            "#tsd_wizard",
        ]
    });

});
