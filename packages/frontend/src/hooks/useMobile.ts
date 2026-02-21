import { useState, useEffect } from 'react';

interface MobileBreakpoints {
  isMobile: boolean;
  isTablet: boolean;
  isDesktop: boolean;
}

/**
 * Hook to detect device type based on viewport width
 * Mobile: < 768px
 * Tablet: 768px - 1024px
 * Desktop: > 1024px
 */
export function useMobile(): MobileBreakpoints {
  const [breakpoints, setBreakpoints] = useState<MobileBreakpoints>({
    isMobile: false,
    isTablet: false,
    isDesktop: true,
  });

  useEffect(() => {
    // Define media queries
    const mobileQuery = window.matchMedia('(max-width: 767px)');
    const tabletQuery = window.matchMedia('(min-width: 768px) and (max-width: 1023px)');
    const desktopQuery = window.matchMedia('(min-width: 1024px)');

    // Handler to update state
    const updateBreakpoints = () => {
      setBreakpoints({
        isMobile: mobileQuery.matches,
        isTablet: tabletQuery.matches,
        isDesktop: desktopQuery.matches,
      });
    };

    // Set initial values
    updateBreakpoints();

    // Add listeners for changes
    mobileQuery.addEventListener('change', updateBreakpoints);
    tabletQuery.addEventListener('change', updateBreakpoints);
    desktopQuery.addEventListener('change', updateBreakpoints);

    // Cleanup
    return () => {
      mobileQuery.removeEventListener('change', updateBreakpoints);
      tabletQuery.removeEventListener('change', updateBreakpoints);
      desktopQuery.removeEventListener('change', updateBreakpoints);
    };
  }, []);

  return breakpoints;
}
